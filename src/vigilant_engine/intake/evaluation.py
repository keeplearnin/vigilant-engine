"""Before/after evaluation of the intake agent.

Runs a fixed set of ideas ("cases") through the planner and scores each backlog with
deterministic checks, so a prompt change can be judged on evidence:

* quality metrics: gate findings, untested claims, numeric acceptance criteria, vague words,
  oversized stories, walking skeleton, Definition of Done;
* per-case expectations: regular expressions for things a reviewer would demand for that
  idea (for example session revocation after a password reset).

Keyword expectations are an approximation of a human review, not a replacement for it.

Usage (needs Claude API access for `run`; `score` and `compare` are free):
  uv run python evals/intake/run_eval.py run --prompt-version v1 --label v1
  uv run python evals/intake/run_eval.py run --prompt-version v2 --label v2
  uv run python evals/intake/run_eval.py score --backlog runs/<id>/backlog.json \\
      --case password-reset-sms --label v1
  uv run python evals/intake/run_eval.py compare evals/results/v1 evals/results/v2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .models import Backlog, IdeaBrief
from .quality import Finding, lint

VAGUE = re.compile(
    r"\b(fast|quick(ly)?|easy|easily|user[- ]friendly|intuitive|seamless(ly)?|"
    r"appropriate(ly)?|properly|robust|efficient(ly)?|as needed|reasonable|adequate)\b",
    re.I,
)
WALKING_SKELETON = re.compile(r"walking skeleton|thin end[- ]to[- ]end|end[- ]to[- ]end skeleton",
                              re.I)

# metric -> +1 when higher is better, -1 when lower is better, 0 when neutral
DIRECTION = {
    "expectations_met_pct": 1,
    "ac_with_number_pct": 1,
    "walking_skeleton": 1,
    "dod_recorded": 1,
    "lint_errors": -1,
    "lint_warnings": -1,
    "untested_claims": -1,
    "broad_stories": -1,
    "stories_over_5_points": -1,
    "vague_words": -1,
    "stories": 0,
    "story_points": 0,
    "avg_ac_per_story": 0,
}


@dataclass
class Expectation:
    name: str
    pattern: str

    def met(self, text: str) -> bool:
        return re.search(self.pattern, text, re.I | re.S) is not None


@dataclass
class Case:
    id: str
    idea: str
    context: str = ""
    expect: list[Expectation] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    root = path.parent
    cases = []
    for raw in json.loads(path.read_text(encoding="utf-8")):
        idea = raw.get("idea") or (root / raw["idea_file"]).read_text(encoding="utf-8")
        cases.append(Case(
            id=raw["id"], idea=idea,
            expect=[Expectation(e["name"], e["pattern"]) for e in raw.get("expect", [])],
        ))
    return cases


def backlog_text(backlog: Backlog) -> str:
    return json.dumps(backlog.model_dump(), ensure_ascii=False)


def score(backlog: Backlog, findings: list[Finding] | None = None,
          expectations: list[Expectation] | None = None) -> dict[str, Any]:
    findings = lint(backlog) if findings is None else findings
    expectations = expectations or []
    stories = [s for _, _, s in backlog.iter_stories()]
    acs = [f"{ac.given} {ac.when} {ac.then}" for s in stories for ac in s.acceptance_criteria]
    text = backlog_text(backlog)
    met = [e.name for e in expectations if e.met(text)]
    rule_count = lambda rule: sum(1 for f in findings if f.rule == rule)  # noqa: E731
    return {
        "stories": len(stories),
        "story_points": sum(s.story_points for s in stories),
        "avg_ac_per_story": round(len(acs) / len(stories), 2) if stories else 0,
        "lint_errors": sum(1 for f in findings if f.severity == "error"),
        "lint_warnings": sum(1 for f in findings if f.severity == "warning"),
        "untested_claims": rule_count("untested-claim"),
        "broad_stories": rule_count("story-too-broad"),
        "stories_over_5_points": sum(1 for s in stories if s.story_points > 5),
        "ac_with_number_pct": round(100 * sum(1 for a in acs if re.search(r"\d", a))
                                    / len(acs)) if acs else 0,
        "vague_words": sum(len(VAGUE.findall(a)) for a in acs),
        "walking_skeleton": int(any(WALKING_SKELETON.search(f"{s.title} {s.description}")
                                    for s in stories)),
        "dod_recorded": int(any(a.lower().startswith("definition of done")
                                for a in backlog.assumptions)),
        "expectations_met_pct": round(100 * len(met) / len(expectations)) if expectations
        else 100,
        "expectations_missed": [e.name for e in expectations if e.name not in met],
    }


def compare(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> str:
    """Side-by-side table per case; marks each change as better (+) or worse (-)."""
    lines: list[str] = []
    better = worse = 0
    for case_id in sorted(set(before) | set(after)):
        a, b = before.get(case_id), after.get(case_id)
        lines.append(f"\n{case_id}")
        if a is None or b is None:
            lines.append(f"  only in {'after' if a is None else 'before'}")
            continue
        for metric, direction in DIRECTION.items():
            va, vb = a.get(metric), b.get(metric)
            mark = ""
            if direction and va != vb and None not in (va, vb):
                good = (vb > va) == (direction > 0)
                mark = "  + better" if good else "  - worse"
                better += good
                worse += not good
            lines.append(f"  {metric:<24} {va!s:>8} -> {vb!s:<8}{mark}")
        for label, metrics in (("before", a), ("after", b)):
            if metrics.get("expectations_missed"):
                lines.append(f"  missed ({label}): {', '.join(metrics['expectations_missed'])}")
    lines.append(f"\nSummary: {better} metric(s) better, {worse} worse.")
    return "\n".join(lines)


# --- command line ----------------------------------------------------------------------

def _load_results(folder: Path) -> dict[str, dict[str, Any]]:
    results = {}
    for metrics_file in sorted(folder.glob("*/metrics.json")):
        results[metrics_file.parent.name] = json.loads(metrics_file.read_text(encoding="utf-8"))
    if not results:
        raise SystemExit(f"No results found in {folder}")
    return results


def main(argv: list[str] | None = None, *, llm: Any = None, stdin: TextIO = sys.stdin,
         out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="run_eval", description="Evaluate the intake agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    default_cases = Path("evals/intake/cases.json")

    run = sub.add_parser("run", help="Plan every case and score it (uses Claude; costs money)")
    run.add_argument("--label", required=True)
    run.add_argument("--prompt-version", default="v2")
    run.add_argument("--context-file", type=Path, help="Extra context for every case")
    run.add_argument("--cases", type=Path, default=default_cases)
    run.add_argument("--only", action="append", help="Run only these case ids")
    run.add_argument("--effort", default="high")
    run.add_argument("--model", default=None)
    run.add_argument("--results-dir", type=Path, default=Path("evals/results"))
    run.add_argument("--yes", action="store_true", help="Skip the cost confirmation")

    sc = sub.add_parser("score", help="Score an existing backlog.json (free)")
    sc.add_argument("--backlog", type=Path, required=True)
    sc.add_argument("--case", required=True)
    sc.add_argument("--label", required=True)
    sc.add_argument("--cases", type=Path, default=default_cases)
    sc.add_argument("--results-dir", type=Path, default=Path("evals/results"))

    cmp_ = sub.add_parser("compare", help="Compare two result folders (free)")
    cmp_.add_argument("before", type=Path)
    cmp_.add_argument("after", type=Path)

    args = parser.parse_args(argv)

    if args.cmd == "compare":
        print(compare(_load_results(args.before), _load_results(args.after)), file=out)
        return 0

    cases = {c.id: c for c in load_cases(args.cases)}

    if args.cmd == "score":
        case = cases.get(args.case)
        if case is None:
            print(f"Unknown case {args.case!r}. Known: {', '.join(sorted(cases))}", file=out)
            return 1
        backlog = Backlog.model_validate_json(args.backlog.read_text(encoding="utf-8"))
        metrics = score(backlog, expectations=case.expect)
        target = args.results_dir / args.label / case.id
        target.mkdir(parents=True, exist_ok=True)
        (target / "backlog.json").write_text(json.dumps(backlog.model_dump(), indent=1))
        (target / "metrics.json").write_text(json.dumps(metrics, indent=2))
        print(json.dumps(metrics, indent=2), file=out)
        return 0

    # run
    selected = [cases[c] for c in (args.only or cases)]
    if not args.yes:
        print(f"This plans {len(selected)} idea(s) with Claude at effort '{args.effort}' "
              f"(roughly a few dollars each). Continue? [y/N] ", end="", file=out, flush=True)
        if stdin.readline().strip().lower() not in ("y", "yes"):
            print("Cancelled.", file=out)
            return 1

    from ..llm import ClaudeLLM
    from .planner import BacklogPlanner

    llm = llm or ClaudeLLM(model=args.model)
    planner = BacklogPlanner(llm, effort=args.effort, prompt_version=args.prompt_version)
    context = args.context_file.read_text(encoding="utf-8") if args.context_file else ""
    folder = args.results_dir / args.label
    summary = {"label": args.label, "prompt_version": args.prompt_version,
               "context_file": str(args.context_file) if args.context_file else None,
               "model": llm.model, "started": datetime.now(UTC).isoformat(), "cases": {}}
    for case in selected:
        print(f"[{case.id}] planning...", file=out, flush=True)
        target = folder / case.id
        target.mkdir(parents=True, exist_ok=True)
        try:
            result = planner.plan(IdeaBrief(idea=case.idea, context=context))
        except Exception as exc:  # keep going; record the failure
            summary["cases"][case.id] = {"error": str(exc)}
            print(f"[{case.id}] failed: {exc}", file=out)
            continue
        metrics = score(result.backlog, result.findings, case.expect)
        (target / "backlog.json").write_text(json.dumps(result.backlog.model_dump(), indent=1))
        (target / "metrics.json").write_text(json.dumps(metrics, indent=2))
        (target / "model-calls.json").write_text(json.dumps(result.calls, indent=2))
        summary["cases"][case.id] = metrics
        print(f"[{case.id}] expectations {metrics['expectations_met_pct']}%, "
              f"untested claims {metrics['untested_claims']}, "
              f"lint errors {metrics['lint_errors']}", file=out)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Results: {folder}", file=out)
    return 0
