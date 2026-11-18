"""Command line for the idea-intake agent.

  vigilant intake plan     idea -> reviewed backlog (backlog.md / backlog.json) in a run folder
  vigilant intake publish  approved backlog -> Jira (dry run unless --apply)
  vigilant intake verify   check the run's hash-chained event log
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

import anthropic
from pydantic import ValidationError

from ..evidence import EvidenceError, RunStore, digest
from ..intake import Backlog, BacklogPlanner, IdeaBrief, Limits, PlannerError, lint
from ..intake.prompts import DEFAULT_PROMPT_VERSION, PROMPTS
from ..intake.quality import has_errors
from ..intake.render import to_markdown
from ..integrations.jira import (
    JiraClient,
    JiraConfig,
    JiraError,
    JiraPublisher,
    PublishError,
    PublishOptions,
    PublishPlan,
)
from ..llm import ClaudeLLM, LLMError, StructuredLLM

EXIT_OK, EXIT_BLOCKED, EXIT_ERROR, EXIT_INTERRUPTED = 0, 2, 1, 130


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def read_text_file(path: Path, what: str) -> str:
    """Read a UTF-8 text file, turning common mistakes into one clear sentence."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UserFacingError(f"The {what} {path} was not found.") from None
    except IsADirectoryError:
        raise UserFacingError(f"The {what} {path} is a folder, not a file.") from None
    except UnicodeDecodeError:
        raise UserFacingError(f"The {what} {path} is not UTF-8 text. Save it as UTF-8 "
                              "(plain text or Markdown) and try again.") from None


class UserFacingError(Exception):
    """An input problem the user can fix; printed without a traceback."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vigilant", description="vigilant-engine command line")
    sub = parser.add_subparsers(dest="area", required=True)
    intake = sub.add_parser("intake", help="Turn an idea into Jira epics, features and stories")
    cmds = intake.add_subparsers(dest="command", required=True)

    plan = cmds.add_parser("plan", help="Draft and quality-check a backlog from an idea")
    src = plan.add_mutually_exclusive_group(required=True)
    src.add_argument("--idea", help="The idea as text")
    src.add_argument("--idea-file", type=Path, help="File containing the idea")
    plan.add_argument("--context-file", type=Path, action="append", default=[],
                      help="Background or standards file (repeatable; standards first). "
                           "See docs/agents/context-guidelines.md")
    plan.add_argument("--interactive", action="store_true",
                      help="Answer up to 5 clarifying questions before planning")
    plan.add_argument("--runs-dir", type=Path, default=Path("runs"))
    plan.add_argument("--provider", choices=["anthropic", "bedrock"], default="anthropic")
    plan.add_argument("--model", default=None, help="Default: claude-opus-5-5")
    plan.add_argument("--aws-region", default=None, help="Bedrock region (or set AWS_REGION)")
    plan.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                      default="high")
    plan.add_argument("--prompt-version", choices=sorted(PROMPTS), default=DEFAULT_PROMPT_VERSION,
                      help="System prompt version (for before/after comparisons)")
    plan.add_argument("--max-epics", type=positive_int, default=Limits.max_epics)
    plan.add_argument("--max-features-per-epic", type=positive_int,
                      default=Limits.max_features_per_epic)
    plan.add_argument("--max-stories-per-feature", type=positive_int,
                      default=Limits.max_stories_per_feature)
    plan.add_argument("--max-total-issues", type=positive_int,
                      default=Limits.max_total_issues)

    pub = cmds.add_parser("publish", help="Create the reviewed backlog in Jira")
    pub.add_argument("--run", type=Path, required=True, help="Run folder from 'intake plan'")
    pub.add_argument("--project", required=True, help="Jira project key, e.g. CLAIMS")
    pub.add_argument("--apply", action="store_true",
                     help="Create the issues. Without this flag nothing is written.")
    pub.add_argument("--yes", action="store_true",
                     help="Skip the typed confirmation (requires --approver)")
    pub.add_argument("--approver", default=None,
                     help="Name recorded as the approver (default: your OS user name)")
    pub.add_argument("--feature-mode", choices=["auto", "issue-type", "label"], default="auto",
                     help="'label' when your project has no Feature issue type")
    pub.add_argument("--epic-type", default="Epic")
    pub.add_argument("--feature-type", default="Feature")
    pub.add_argument("--story-type", default="Story")
    pub.add_argument("--story-points-field", default=None,
                     help="Custom field ID for story points, e.g. customfield_10016")
    pub.add_argument("--label", action="append", default=[], help="Extra label (repeatable)")
    pub.add_argument("--max-issues", type=positive_int, default=150)

    imp = cmds.add_parser("import", help="Start a run from a backlog JSON written by a person")
    imp.add_argument("--backlog-file", type=Path, required=True)
    imp.add_argument("--runs-dir", type=Path, default=Path("runs"))

    ver = cmds.add_parser("verify", help="Verify a run's event log")
    ver.add_argument("--run", type=Path, required=True)
    ver.add_argument("--expect-head", default=None,
                     help="A record hash taken from elsewhere (e.g. a Jira issue footer) that "
                          "must still be in the log")
    return parser


# --- plan --------------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace, *, llm: StructuredLLM | None = None,
             stdin: TextIO = sys.stdin, out: TextIO = sys.stdout) -> int:
    try:
        idea = args.idea if args.idea else read_text_file(args.idea_file, "idea file")
        context = "\n\n---\n\n".join(
            read_text_file(path, "context file").strip() for path in args.context_file)
    except UserFacingError as exc:
        print(str(exc), file=out)
        return EXIT_ERROR
    if not idea.strip():
        print("The idea is empty.", file=out)
        return EXIT_ERROR

    limits = Limits(args.max_epics, args.max_features_per_epic, args.max_stories_per_feature,
                    args.max_total_issues)
    if llm is None:
        try:  # before the run folder exists, so a bad setup leaves nothing behind
            llm = ClaudeLLM(provider=args.provider, model=args.model,
                            aws_region=args.aws_region)
        except (LLMError, anthropic.AnthropicError) as exc:
            print(f"Cannot reach Claude: {exc}", file=out)
            return EXIT_ERROR
    store = RunStore(args.runs_dir)

    def save_attempt(attempt: int, backlog: Backlog, findings: list) -> None:
        # written immediately, so a later failure or Ctrl+C never loses a paid draft
        store.write_json(f"attempts/attempt-{attempt}.json", backlog.model_dump())

    planner = BacklogPlanner(llm, limits=limits, effort=args.effort, events=store.events,
                             prompt_version=args.prompt_version, on_attempt=save_attempt)
    brief = IdeaBrief(idea=idea, context=context)

    store.write_text("idea.md", idea)
    store.write_json("limits.json", asdict(limits))
    store.events.append("run.started", actor=f"human:{getpass.getuser()}",
                        payload={"idea_digest": digest(idea.encode()), "model": llm.model,
                                 "prompt_version": args.prompt_version,
                                 "context_digest": digest(context.encode()) if context else None,
                                 "limits": asdict(limits)})
    print(f"Run {store.run_id}: planning with {llm.model}...", file=out)

    try:
        if args.interactive:
            questions = planner.clarify(brief).questions
            answers = []
            for i, q in enumerate(questions, 1):
                print(f"\nQ{i}. {q.question}\n    Why: {q.why_it_matters}\n"
                      f"    Press Enter to accept: {q.default_assumption}", file=out)
                print("    > ", end="", file=out, flush=True)
                reply = stdin.readline().strip()
                answers.append((q.question, reply or f"(assumed) {q.default_assumption}"))
            brief = IdeaBrief(idea=idea, context=context, answers=answers)
            store.events.append("intake.clarified", actor=f"human:{getpass.getuser()}",
                                payload={"answers": answers})
        result = planner.plan(brief)
    except KeyboardInterrupt:
        store.events.append("run.failed", actor=f"human:{getpass.getuser()}",
                            payload={"error": "interrupted", "kind": "KeyboardInterrupt"})
        print(f"\nStopped. Any drafts received so far are in {store.path('attempts')}.",
              file=out)
        return EXIT_INTERRUPTED
    except (LLMError, PlannerError, anthropic.AnthropicError) as exc:
        message = _claude_error_message(exc)
        store.events.append("run.failed", actor="system:intake",
                            payload={"error": message, "kind": type(exc).__name__})
        print(f"Planning failed: {message}", file=out)
        print(f"Partial run kept for the record: {store.dir}", file=out)
        return EXIT_ERROR

    store.write_json("brief.json", brief.model_dump())
    store.write_json("backlog.json", result.backlog.model_dump())
    store.write_json("lint.json", [f.to_dict() for f in result.findings])
    store.write_json("model-calls.json", result.calls)
    store.write_text("backlog.md", to_markdown(result.backlog, result.findings, store.run_id))
    planned_digest = digest(result.backlog.model_dump())
    store.events.append("intake.planned", actor="agent:intake",
                        payload={"backlog_digest": planned_digest,
                                 "counts": result.backlog.counts(),
                                 "attempts": result.attempts,
                                 "chosen_attempt": result.chosen_attempt,
                                 "repair_error": result.repair_error,
                                 "publishable": result.publishable})

    c = result.backlog.counts()
    print(f"\n{result.backlog.product_name}: {c['epics']} epics, {c['features']} features, "
          f"{c['stories']} stories, {c['story_points']} points "
          f"({result.attempts} attempt(s)).", file=out)
    if result.chosen_attempt != result.attempts:
        print(f"Kept attempt {result.chosen_attempt}: it had the fewest quality-gate errors.",
              file=out)
    if result.repair_error:
        print(f"Note: {result.repair_error}. The best draft so far was kept.", file=out)
    errors = [f for f in result.findings if f.severity == "error"]
    warnings = [f for f in result.findings if f.severity == "warning"]
    print(f"Quality gate: {len(errors)} error(s), {len(warnings)} warning(s).", file=out)
    print(f"Review: {store.path('backlog.md')}", file=out)
    print(f"Edit if needed: {store.path('backlog.json')}", file=out)
    if errors:
        print("Blocked: fix the errors in backlog.json, then run publish.", file=out)
        print(f"Evidence head: {store.events.head}", file=out)
        return EXIT_BLOCKED
    print(f"Next: vigilant intake publish --run {store.dir} --project <KEY>", file=out)
    print(f"Evidence head: {store.events.head}", file=out)
    return EXIT_OK


# --- publish -----------------------------------------------------------------------------

def cmd_publish(args: argparse.Namespace, *, client: JiraClient | None = None,
                stdin: TextIO = sys.stdin, out: TextIO = sys.stdout) -> int:
    if args.yes and not args.approver:
        print("--yes skips the typed confirmation, so name the approver with --approver.",
              file=out)
        return EXIT_ERROR
    try:
        store = RunStore.open(args.run)
        backlog = Backlog.model_validate_json(
            store.path("backlog.json").read_text(encoding="utf-8"))
        limits = _load_limits(store)
    except FileNotFoundError as exc:
        print(f"Not found: {exc.filename or exc}. Check the --run folder.", file=out)
        return EXIT_ERROR
    except (ValidationError, json.JSONDecodeError) as exc:
        print("backlog.json is not a valid backlog. Fix these problems and try again:",
              file=out)
        print(_validation_summary(exc), file=out)
        return EXIT_BLOCKED
    findings = lint(backlog, limits)
    if has_errors(findings):
        for f in findings:
            if f.severity == "error":
                print(f"  error {f.ref}: {f.message}", file=out)
        print("Blocked by the quality gate. Fix backlog.json and try again.", file=out)
        return EXIT_BLOCKED

    current = digest(backlog.model_dump())
    planned = _planned_digest(store)
    if planned and planned != current:
        store.events.append("intake.backlog_edited", actor=f"human:{getpass.getuser()}",
                            payload={"from": planned, "to": current})

    owns_client = client is None
    if client is None:
        try:
            client = JiraClient(JiraConfig.from_env())
        except JiraError as exc:
            if args.apply:
                print(f"Cannot connect to Jira: {exc}", file=out)
                return EXIT_ERROR
            print(f"No Jira connection ({exc}). Showing an offline preview.", file=out)
    try:
        return _publish(args, store, backlog, current, client, stdin, out)
    finally:
        if owns_client and client is not None:
            client.close()


def _publish(args: argparse.Namespace, store: RunStore, backlog: Backlog, current: str,
             client: JiraClient | None, stdin: TextIO, out: TextIO) -> int:

    opts = PublishOptions(
        project_key=args.project, run_id=store.run_id, epic_type=args.epic_type,
        feature_type=args.feature_type, story_type=args.story_type,
        feature_mode=args.feature_mode, story_points_field=args.story_points_field,
        extra_labels=tuple(args.label), max_issues=args.max_issues,
    )
    publisher = JiraPublisher(client, store.path("jira-manifest.json"), events=store.events)

    try:
        plan = publisher.prepare(backlog, opts)
        _print_plan(plan, out)
        if not args.apply:
            print("\nDry run: nothing was created. Add --apply to create these issues.", file=out)
            return EXIT_OK

        os_user = getpass.getuser()
        approver = args.approver or os_user
        if not args.yes:
            print(f"\nType the project key ({args.project}) to create "
                  f"{len(plan.issues)} issues: ", end="", file=out, flush=True)
            if stdin.readline().strip() != args.project:
                print("Not confirmed. Nothing was created.", file=out)
                return EXIT_BLOCKED
        # Local CLI identity is self-asserted; record how approval was given so reviewers
        # can weigh it. Service mode would take the approver from SSO instead.
        approval = store.events.append(
            "intake.approved", actor=f"human:{approver}",
            payload={"project": args.project, "backlog_digest": current,
                     "plan_digest": plan.digest, "issues": len(plan.issues),
                     "method": "--yes flag" if args.yes else "typed project key",
                     "os_user": os_user, "interactive": bool(stdin.isatty())})
        # exactly the plan shown and approved; the approval hash goes into every issue
        report = publisher.execute(plan, anchor=approval["hash"])
    except (PublishError, JiraError) as exc:
        store.events.append("jira.failed", actor="system:jira-publisher",
                            payload={"error": str(exc)})
        print(f"Publish failed: {exc}", file=out)
        if args.apply:
            print("Issues created so far are saved in jira-manifest.json; re-run to resume.",
                  file=out)
        return EXIT_ERROR

    print(f"\nCreated {len(report.created)} issue(s); "
          f"{len(report.skipped)} already existed; "
          f"{len(report.reconciled)} recovered after an uncertain write.", file=out)
    for local_id, key in {**report.skipped, **report.reconciled, **report.created}.items():
        print(f"  {local_id:<10} {key}", file=out)
    print(f"Approval record (also in each new issue): {approval['hash']}", file=out)
    print(f"Evidence head: {store.events.head}", file=out)
    return EXIT_OK


def _planned_digest(store: RunStore) -> str | None:
    found = None
    for event in store.events.events():
        if event.get("type") == "intake.planned":
            found = event.get("payload", {}).get("backlog_digest")
    return found


def _load_limits(store: RunStore) -> Limits:
    """The limits the run was planned with, so publish judges it by the same rules."""
    path = store.path("limits.json")
    if not path.exists():
        return Limits()
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {k: int(v) for k, v in data.items() if k in Limits.__dataclass_fields__}
    return Limits(**known)


def _validation_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors()[:10]:
            where = ".".join(str(p) for p in err["loc"]) or "backlog"
            lines.append(f"  {where}: {err['msg']}")
        more = len(exc.errors()) - 10
        if more > 0:
            lines.append(f"  ... and {more} more")
        return "\n".join(lines)
    if isinstance(exc, json.JSONDecodeError):
        return f"  line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return f"  {exc}"


def _claude_error_message(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude rejected the API credentials. Check ANTHROPIC_API_KEY or `ant auth login`."
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude rate limit reached. Wait a minute and run plan again."
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return f"Claude is temporarily unavailable ({exc.status_code}). Try again shortly."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not connect to Claude. Check the network and try again."
    return str(exc)


def _print_plan(plan: PublishPlan, out: TextIO) -> None:
    for w in plan.warnings:
        print(f"Note: {w}", file=out)
    counts: dict[str, int] = {}
    for issue in plan.issues:
        counts[issue.issue_type] = counts.get(issue.issue_type, 0) + 1
    summary = ", ".join(f"{n} {t}" for t, n in counts.items())
    print(f"Plan ({plan.feature_mode} mode, project {plan.project_key}): {summary}", file=out)
    for issue in plan.issues:
        indent = {"epic": "", "feature": "  ", "story": "    "}[issue.level]
        if plan.feature_mode == "label" and issue.level == "story":
            indent = "  "
        pts = f" [{issue.story_points}]" if issue.story_points else ""
        print(f"  {indent}{issue.local_id:<9} {issue.issue_type:<8} {issue.summary}{pts}",
              file=out)


# --- import ------------------------------------------------------------------------------

def cmd_import(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    """Run a hand-written backlog through the same gate, review files and evidence trail."""
    try:
        backlog = Backlog.model_validate_json(args.backlog_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"File not found: {args.backlog_file}", file=out)
        return EXIT_ERROR
    except (ValidationError, json.JSONDecodeError) as exc:
        print(f"{args.backlog_file} is not a valid backlog:\n{_validation_summary(exc)}",
              file=out)
        return EXIT_ERROR

    findings = lint(backlog)
    store = RunStore(args.runs_dir)
    store.write_json("limits.json", asdict(Limits()))
    author = f"human:{getpass.getuser()}"
    store.events.append("run.started", actor=author,
                        payload={"source": "import", "file": str(args.backlog_file)})
    store.write_json("backlog.json", backlog.model_dump())
    store.write_json("lint.json", [f.to_dict() for f in findings])
    store.write_text("backlog.md", to_markdown(backlog, findings, store.run_id))
    store.events.append("intake.planned", actor=author,
                        payload={"backlog_digest": digest(backlog.model_dump()),
                                 "counts": backlog.counts(), "source": "import",
                                 "publishable": not has_errors(findings)})
    c = backlog.counts()
    print(f"Run {store.run_id}: imported {c['epics']} epics, {c['features']} features, "
          f"{c['stories']} stories.", file=out)
    print(f"Review: {store.path('backlog.md')}", file=out)
    if has_errors(findings):
        for f in findings:
            if f.severity == "error":
                print(f"  error {f.ref}: {f.message}", file=out)
        return EXIT_BLOCKED
    print(f"Next: vigilant intake publish --run {store.dir} --project <KEY>", file=out)
    print(f"Evidence head: {store.events.head}", file=out)
    return EXIT_OK


# --- verify ------------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    try:
        store = RunStore.open(args.run)
    except FileNotFoundError as exc:
        print(str(exc), file=out)
        return EXIT_ERROR
    ok, message = store.events.verify(expect_head=args.expect_head)
    print(message, file=out)
    return EXIT_OK if ok else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"plan": cmd_plan, "publish": cmd_publish, "import": cmd_import,
                "verify": cmd_verify}
    try:
        return handlers[args.command](args)
    except EvidenceError as exc:
        print(f"Stopped: {exc}\nRun `vigilant intake verify --run <folder>` for details.")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
