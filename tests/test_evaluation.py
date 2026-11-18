"""Prompt versions, new quality warnings and the before/after evaluation harness."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from vigilant_engine.intake import Backlog, BacklogPlanner, IdeaBrief, lint, prompts
from vigilant_engine.intake.evaluation import Expectation, compare, load_cases, main, score
from vigilant_engine.interfaces.cli import build_parser, cmd_plan

from .conftest import FakeLLM

REPO = Path(__file__).resolve().parents[1]


def rules(findings, severity="warning"):
    return {f.rule for f in findings if f.severity == severity}


# --- quality gate warnings -----------------------------------------------------------------

def test_untested_claim_is_flagged(make_backlog):
    def mutate(d):
        d["epics"][0]["features"][0]["stories"][0]["description"] = (
            "Phone numbers are encrypted at rest and the user is notified.")
    findings = lint(Backlog.model_validate(make_backlog(mutate)))
    messages = [f.message for f in findings if f.rule == "untested-claim"]
    assert len(messages) == 2 and any("encrypted" in m for m in messages)


def test_claim_tested_by_criterion_is_not_flagged(make_backlog):
    def mutate(d):
        story = d["epics"][0]["features"][0]["stories"][0]
        story["description"] = "Phone numbers are encrypted at rest."
        story["acceptance_criteria"][0]["then"] = "the stored number is encrypted"
    assert "untested-claim" not in rules(lint(Backlog.model_validate(make_backlog(mutate))))


def test_broad_large_story_is_flagged(make_backlog):
    def mutate(d):
        story = d["epics"][0]["features"][0]["stories"][0]
        story["title"], story["story_points"] = "Set password and write to directory", 8
    assert "story-too-broad" in rules(lint(Backlog.model_validate(make_backlog(mutate))))


# --- prompt versions ---------------------------------------------------------------------

def test_v2_adds_quality_bar_and_keeps_v1():
    assert "Quality bar" in prompts.SYSTEM_PROMPT_V2
    assert "Quality bar" not in prompts.SYSTEM_PROMPT_V1
    assert prompts.SYSTEM_PROMPT == prompts.SYSTEM_PROMPT_V2
    assert prompts.SYSTEM_PROMPT_V2.startswith(prompts.SYSTEM_PROMPT_V1.split("Honesty:")[0])


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_planner_sends_selected_prompt(backlog_dict, version):
    llm = FakeLLM(backlog_dict)
    result = BacklogPlanner(llm, prompt_version=version).plan(IdeaBrief(idea="x"))
    assert llm.requests[0]["system"] == prompts.PROMPTS[version]
    assert result.calls[0]["prompt_version"] == version


def test_unknown_prompt_version_is_rejected():
    with pytest.raises(ValueError, match="v9"):
        BacklogPlanner(FakeLLM(), prompt_version="v9")


def test_cli_records_prompt_version(tmp_path, backlog_dict):
    args = build_parser().parse_args(["intake", "plan", "--idea", "x", "--prompt-version", "v1",
                                      "--runs-dir", str(tmp_path)])
    llm = FakeLLM(backlog_dict)
    assert cmd_plan(args, llm=llm, out=io.StringIO()) == 0
    assert llm.requests[0]["system"] == prompts.SYSTEM_PROMPT_V1
    log = next(tmp_path.iterdir()) / "events.jsonl"
    assert '"prompt_version": "v1"' in log.read_text()


# --- scoring and comparison ------------------------------------------------------------------

def test_score_metrics_and_expectations(backlog_dict):
    backlog = Backlog.model_validate(backlog_dict)
    metrics = score(backlog, expectations=[Expectation("docs", r"documents are missing"),
                                           Expectation("revoke", r"revok")])
    assert metrics["stories"] == 3 and metrics["story_points"] == 10
    assert metrics["expectations_met_pct"] == 50 and metrics["expectations_missed"] == ["revoke"]
    assert metrics["lint_errors"] == 0 and metrics["walking_skeleton"] == 0


def test_compare_marks_direction():
    before = {"c": {"untested_claims": 3, "expectations_met_pct": 20, "stories": 10}}
    after = {"c": {"untested_claims": 1, "expectations_met_pct": 10, "stories": 12}}
    table = compare(before, after)
    assert "untested_claims" in table and "+ better" in table and "- worse" in table
    assert "Summary: 1 metric(s) better, 1 worse." in table


def test_repo_cases_load_and_reference_real_files():
    cases = load_cases(REPO / "evals/intake/cases.json")
    assert {c.id for c in cases} == {"password-reset-sms", "claim-status-updates",
                                     "address-change"}
    assert all(c.idea.strip() and c.expect for c in cases)


# --- command line (free paths, and run with a fake model) ------------------------------------

def test_score_then_compare(tmp_path, backlog_dict, make_backlog):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(backlog_dict))
    b.write_text(json.dumps(make_backlog(lambda d: d["epics"][0]["features"][0]["stories"][0]
                                         .update(description="Sessions are revoked."))))
    cases = str(REPO / "evals/intake/cases.json")
    for path, label in ((a, "before"), (b, "after")):
        assert main(["score", "--backlog", str(path), "--case", "password-reset-sms",
                     "--label", label, "--cases", cases, "--results-dir", str(tmp_path / "r")],
                    out=io.StringIO()) == 0
    out = io.StringIO()
    main(["compare", str(tmp_path / "r/before"), str(tmp_path / "r/after")], out=out)
    assert "password-reset-sms" in out.getvalue() and "untested_claims" in out.getvalue()


def test_run_asks_before_spending(tmp_path):
    out = io.StringIO()
    code = main(["run", "--label", "x", "--cases", str(REPO / "evals/intake/cases.json"),
                 "--results-dir", str(tmp_path)], stdin=io.StringIO("n\n"), out=out)
    assert code == 1 and "Cancelled" in out.getvalue() and not any(tmp_path.iterdir())


def test_run_with_fake_model(tmp_path, backlog_dict):
    out = io.StringIO()
    code = main(["run", "--label", "v2", "--only", "address-change", "--yes",
                 "--cases", str(REPO / "evals/intake/cases.json"),
                 "--results-dir", str(tmp_path)], llm=FakeLLM(backlog_dict), out=out)
    assert code == 0, out.getvalue()
    summary = json.loads((tmp_path / "v2/summary.json").read_text())
    assert summary["prompt_version"] == "v2" and "address-change" in summary["cases"]
    assert (tmp_path / "v2/address-change/metrics.json").exists()
