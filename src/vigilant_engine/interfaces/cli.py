"""Command line for the idea-intake agent.

  vigilant intake plan     idea -> reviewed backlog (backlog.md / backlog.json) in a run folder
  vigilant intake publish  approved backlog -> Jira (dry run unless --apply)
  vigilant intake verify   check the run's hash-chained event log
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import TextIO

from ..evidence import RunStore, digest
from ..intake import Backlog, BacklogPlanner, IdeaBrief, Limits, PlannerError, lint
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

EXIT_OK, EXIT_BLOCKED, EXIT_ERROR = 0, 2, 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vigilant", description="vigilant-engine command line")
    sub = parser.add_subparsers(dest="area", required=True)
    intake = sub.add_parser("intake", help="Turn an idea into Jira epics, features and stories")
    cmds = intake.add_subparsers(dest="command", required=True)

    plan = cmds.add_parser("plan", help="Draft and quality-check a backlog from an idea")
    src = plan.add_mutually_exclusive_group(required=True)
    src.add_argument("--idea", help="The idea as text")
    src.add_argument("--idea-file", type=Path, help="File containing the idea")
    plan.add_argument("--context-file", type=Path,
                      help="Optional background: users, constraints, existing systems")
    plan.add_argument("--interactive", action="store_true",
                      help="Answer up to 5 clarifying questions before planning")
    plan.add_argument("--runs-dir", type=Path, default=Path("runs"))
    plan.add_argument("--provider", choices=["anthropic", "bedrock"], default="anthropic")
    plan.add_argument("--model", default=None, help="Default: claude-opus-5-5")
    plan.add_argument("--aws-region", default=None, help="Bedrock region (or set AWS_REGION)")
    plan.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                      default="high")
    plan.add_argument("--max-epics", type=int, default=Limits.max_epics)
    plan.add_argument("--max-features-per-epic", type=int, default=Limits.max_features_per_epic)
    plan.add_argument("--max-stories-per-feature", type=int,
                      default=Limits.max_stories_per_feature)
    plan.add_argument("--max-total-issues", type=int, default=Limits.max_total_issues)

    pub = cmds.add_parser("publish", help="Create the reviewed backlog in Jira")
    pub.add_argument("--run", type=Path, required=True, help="Run folder from 'intake plan'")
    pub.add_argument("--project", required=True, help="Jira project key, e.g. CLAIMS")
    pub.add_argument("--apply", action="store_true",
                     help="Create the issues. Without this flag nothing is written.")
    pub.add_argument("--yes", action="store_true", help="Skip the typed confirmation")
    pub.add_argument("--approver", default=None, help="Recorded as the approver (default: OS user)")
    pub.add_argument("--feature-mode", choices=["auto", "issue-type", "label"], default="auto",
                     help="'label' when your project has no Feature issue type")
    pub.add_argument("--epic-type", default="Epic")
    pub.add_argument("--feature-type", default="Feature")
    pub.add_argument("--story-type", default="Story")
    pub.add_argument("--story-points-field", default=None,
                     help="Custom field ID for story points, e.g. customfield_10016")
    pub.add_argument("--label", action="append", default=[], help="Extra label (repeatable)")
    pub.add_argument("--max-issues", type=int, default=150)

    imp = cmds.add_parser("import", help="Start a run from a backlog JSON written by a person")
    imp.add_argument("--backlog-file", type=Path, required=True)
    imp.add_argument("--runs-dir", type=Path, default=Path("runs"))

    ver = cmds.add_parser("verify", help="Verify a run's event log")
    ver.add_argument("--run", type=Path, required=True)
    return parser


# --- plan --------------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace, *, llm: StructuredLLM | None = None,
             stdin: TextIO = sys.stdin, out: TextIO = sys.stdout) -> int:
    idea = args.idea if args.idea else args.idea_file.read_text(encoding="utf-8")
    context = args.context_file.read_text(encoding="utf-8") if args.context_file else ""
    if not idea.strip():
        print("The idea is empty.", file=out)
        return EXIT_ERROR

    limits = Limits(args.max_epics, args.max_features_per_epic, args.max_stories_per_feature,
                    args.max_total_issues)
    store = RunStore(args.runs_dir)
    llm = llm or ClaudeLLM(provider=args.provider, model=args.model, aws_region=args.aws_region)
    planner = BacklogPlanner(llm, limits=limits, effort=args.effort, events=store.events)
    brief = IdeaBrief(idea=idea, context=context)

    store.write_text("idea.md", idea)
    store.events.append("run.started", actor=f"human:{getpass.getuser()}",
                        payload={"idea_digest": digest(idea.encode()), "model": llm.model,
                                 "limits": limits.__dict__})
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
    except (LLMError, PlannerError) as exc:
        store.events.append("run.failed", actor="system:intake", payload={"error": str(exc)})
        print(f"Planning failed: {exc}", file=out)
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
                                 "publishable": result.publishable})

    c = result.backlog.counts()
    print(f"\n{result.backlog.product_name}: {c['epics']} epics, {c['features']} features, "
          f"{c['stories']} stories, {c['story_points']} points "
          f"({result.attempts} attempt(s)).", file=out)
    errors = [f for f in result.findings if f.severity == "error"]
    warnings = [f for f in result.findings if f.severity == "warning"]
    print(f"Quality gate: {len(errors)} error(s), {len(warnings)} warning(s).", file=out)
    print(f"Review: {store.path('backlog.md')}", file=out)
    print(f"Edit if needed: {store.path('backlog.json')}", file=out)
    if errors:
        print("Blocked: fix the errors in backlog.json, then run publish.", file=out)
        return EXIT_BLOCKED
    print(f"Next: vigilant intake publish --run {store.dir} --project <KEY>", file=out)
    return EXIT_OK


# --- publish -----------------------------------------------------------------------------

def cmd_publish(args: argparse.Namespace, *, client: JiraClient | None = None,
                stdin: TextIO = sys.stdin, out: TextIO = sys.stdout) -> int:
    store = RunStore.open(args.run)
    backlog = Backlog.model_validate(store.read_json("backlog.json"))
    findings = lint(backlog)
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

    if client is None:
        try:
            client = JiraClient(JiraConfig.from_env())
        except JiraError as exc:
            if args.apply:
                print(f"Cannot connect to Jira: {exc}", file=out)
                return EXIT_ERROR
            print(f"No Jira connection ({exc}). Showing an offline preview.", file=out)

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

        approver = args.approver or getpass.getuser()
        if not args.yes:
            print(f"\nType the project key ({args.project}) to create "
                  f"{len(plan.issues)} issues: ", end="", file=out, flush=True)
            if stdin.readline().strip() != args.project:
                print("Not confirmed. Nothing was created.", file=out)
                return EXIT_BLOCKED
        store.events.append("intake.approved", actor=f"human:{approver}",
                            payload={"project": args.project, "backlog_digest": current,
                                     "plan_digest": plan.digest, "issues": len(plan.issues)})
        report = publisher.execute(plan)  # exactly the plan shown and approved
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
    return EXIT_OK


def _planned_digest(store: RunStore) -> str | None:
    import json

    path = store.events.path
    if not path.exists():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if event["type"] == "intake.planned":
                found = event["payload"]["backlog_digest"]
    return found


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
    import json

    from pydantic import ValidationError

    try:
        backlog = Backlog.model_validate_json(args.backlog_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"File not found: {args.backlog_file}", file=out)
        return EXIT_ERROR
    except (ValidationError, json.JSONDecodeError) as exc:
        print(f"{args.backlog_file} is not a valid backlog:\n{exc}", file=out)
        return EXIT_ERROR

    findings = lint(backlog)
    store = RunStore(args.runs_dir)
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
    return EXIT_OK


# --- verify ------------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    ok, message = RunStore.open(args.run).events.verify()
    print(message, file=out)
    return EXIT_OK if ok else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"plan": cmd_plan, "publish": cmd_publish, "import": cmd_import,
                "verify": cmd_verify}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
