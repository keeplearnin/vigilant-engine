"""Tests for the second batch of review fixes: limits, error handling, evidence log."""

from __future__ import annotations

import copy
import io
import json

import anthropic
import httpx2
import pytest

from vigilant_engine.evidence import EventLog, EvidenceError
from vigilant_engine.interfaces.cli import (
    build_parser,
    cmd_plan,
    cmd_publish,
    cmd_verify,
    main,
)

from .conftest import FakeLLM


def plan(tmp_path, llm, *extra):
    args = build_parser().parse_args(["intake", "plan", "--idea", "Track claims",
                                      "--runs-dir", str(tmp_path), *extra])
    out = io.StringIO()
    code = cmd_plan(args, llm=llm, out=out)
    dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    return code, (dirs[0] if dirs else None), out.getvalue()


def publish(run_dir, *extra, out=None):
    args = build_parser().parse_args(["intake", "publish", "--run", str(run_dir),
                                      "--project", "CLM", *extra])
    out = out or io.StringIO()
    return cmd_publish(args, out=out), out.getvalue()


# --- 1. publish uses the limits the run was planned with ---------------------------------

def test_publish_honours_plan_limits(tmp_path, backlog_dict, monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    big = copy.deepcopy(backlog_dict)
    template = big["epics"][0]
    big["epics"] = []
    for n in range(9):  # 9 epics: over the default limit of 8
        epic = copy.deepcopy(template)
        epic["ref"] = f"epic-{n}"
        for f in epic["features"]:
            f["ref"] = f"{f['ref']}-{n}"
            for s in f["stories"]:
                s["ref"] = f"{s['ref']}-{n}"
                s["depends_on"] = [f"{d}-{n}" for d in s["depends_on"]]
        big["epics"].append(epic)

    code, run_dir, out = plan(tmp_path, FakeLLM(big), "--max-epics", "10")
    assert code == 0, out
    assert json.loads((run_dir / "limits.json").read_text())["max_epics"] == 10
    code, out = publish(run_dir)
    assert code == 0, out  # judged by the plan's limits, not the defaults


# --- 2. Claude errors in plan give a clear message and a run.failed event ------------------

def test_claude_connection_error_is_reported(tmp_path):
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.test"))
    code, run_dir, out = plan(tmp_path, FakeLLM(error))
    assert code == 1 and "Could not connect to Claude" in out
    assert "run.failed" in (run_dir / "events.jsonl").read_text()


def test_bad_provider_setup_leaves_no_run_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    args = build_parser().parse_args(["intake", "plan", "--idea", "x", "--provider", "bedrock",
                                      "--runs-dir", str(tmp_path)])
    out = io.StringIO()
    assert cmd_plan(args, out=out) == 1
    assert "Cannot reach Claude" in out.getvalue() and not any(tmp_path.iterdir())


# --- 3. a bad backlog.json or run folder gives a clear message -----------------------------

def test_invalid_backlog_json_is_explained(tmp_path, backlog_dict, monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    _, run_dir, _ = plan(tmp_path, FakeLLM(backlog_dict))
    path = run_dir / "backlog.json"

    path.write_text('{"product_name": "x",}')
    code, out = publish(run_dir)
    assert code == 2 and "line 1" in out

    data = copy.deepcopy(backlog_dict)
    data["epics"][0]["features"][0]["stories"][0]["story_points"] = 4
    path.write_text(json.dumps(data))
    code, out = publish(run_dir)
    assert code == 2 and "story_points" in out


def test_missing_run_folder_is_explained(tmp_path):
    code, out = publish(tmp_path / "nope")
    assert code == 1 and "Not found" in out


# --- 4. a damaged event log is reported, never crashes, and is never extended --------------

@pytest.mark.parametrize("damage", ["garbage", "drop_hash"])
def test_verify_reports_damage_instead_of_crashing(tmp_path, damage):
    log = EventLog(tmp_path / "run" / "events.jsonl")
    log.append("a", actor="t", payload={})
    log.append("b", actor="t", payload={})
    lines = log.path.read_text().splitlines()
    if damage == "garbage":
        lines[1] = lines[1][:20]
    else:
        event = json.loads(lines[1])
        del event["hash"]
        lines[1] = json.dumps(event)
    log.path.write_text("\n".join(lines) + "\n")

    args = build_parser().parse_args(["intake", "verify", "--run", str(tmp_path / "run")])
    out = io.StringIO()
    assert cmd_verify(args, out=out) == 1 and "Event 2" in out.getvalue()
    with pytest.raises(EvidenceError):
        EventLog(log.path).append("c", actor="t", payload={})


def test_commands_stop_cleanly_on_damaged_log(tmp_path, backlog_dict, monkeypatch, capsys):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    _, run_dir, _ = plan(tmp_path, FakeLLM(backlog_dict))
    log = run_dir / "events.jsonl"
    log.write_text(log.read_text().replace("agent:intake", "agent:other", 1))
    code = main(["intake", "publish", "--run", str(run_dir), "--project", "CLM"])
    assert code == 1 and "damaged event log" in capsys.readouterr().out


# --- 5. appends do not re-read the whole log -------------------------------------------------

def test_append_reads_log_once(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(5):
        log.append("seed", actor="t", payload={"i": i})

    reader = EventLog(log.path)
    calls = {"n": 0}
    original = EventLog.events

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(EventLog, "events", counting)
    for i in range(200):
        reader.append("more", actor="t", payload={"i": i})
    assert calls["n"] == 2  # one verify + one head scan, then cached
    monkeypatch.setattr(EventLog, "events", original)
    assert reader.verify()[0]


# --- planner: best attempt, failed repairs, scope drops, size repairs ------------------------

from vigilant_engine.intake import BacklogPlanner, IdeaBrief  # noqa: E402
from vigilant_engine.intake.prompts import repair_prompt  # noqa: E402
from vigilant_engine.intake.quality import Finding  # noqa: E402
from vigilant_engine.llm import LLMError  # noqa: E402


def broken_story(make_backlog, n_errors=1):
    def mutate(d):
        stories = d["epics"][0]["features"][0]["stories"]
        for s in stories[:n_errors]:
            s["user_story"] = "Show status"
    return make_backlog(mutate)


def test_best_attempt_is_kept(make_backlog):
    one_error, two_errors = broken_story(make_backlog, 1), broken_story(make_backlog, 2)
    result = BacklogPlanner(FakeLLM(one_error, two_errors, two_errors)).plan(IdeaBrief(idea="x"))
    assert result.attempts == 3 and result.chosen_attempt == 1
    assert sum(f.severity == "error" for f in result.findings) == 1


def test_failed_repair_keeps_the_draft(make_backlog):
    saved = []
    planner = BacklogPlanner(FakeLLM(broken_story(make_backlog), LLMError("hit max_tokens")),
                             on_attempt=lambda n, b, f: saved.append(n))
    result = planner.plan(IdeaBrief(idea="x"))
    assert result.repair_error and "max_tokens" in result.repair_error
    assert result.backlog.counts()["stories"] == 3 and saved == [1]


def test_scope_drop_is_flagged(make_backlog, backlog_dict):
    first = broken_story(make_backlog)
    shrunk = make_backlog(lambda d: d["epics"][0]["features"][0].update(
        stories=d["epics"][0]["features"][0]["stories"][:1]))
    shrunk["epics"][0]["features"][1]["stories"][0]["depends_on"] = []
    result = BacklogPlanner(FakeLLM(first, shrunk)).plan(IdeaBrief(idea="x"))
    assert any(f.rule == "scope-reduced" for f in result.findings)


def test_size_errors_get_explicit_repair_guidance(backlog_dict):
    from vigilant_engine.intake import Backlog
    backlog = Backlog.model_validate(backlog_dict)
    size = repair_prompt(backlog, [Finding("error", "too-many-issues", "backlog", "too many")])
    other = repair_prompt(backlog, [Finding("error", "user-story-format", "s", "format")])
    assert "merge closely related items" in size and "merge closely" not in other


def test_ctrl_c_during_plan_is_recorded(tmp_path):
    code, run_dir, out = plan(tmp_path, FakeLLM(KeyboardInterrupt()))
    assert code == 130 and "Stopped" in out
    assert "KeyboardInterrupt" in (run_dir / "events.jsonl").read_text()


def test_two_log_instances_keep_the_chain_intact(tmp_path):
    path = tmp_path / "events.jsonl"
    job, request = EventLog(path), EventLog(path)
    job.append("a", actor="t", payload={})
    request.append("b", actor="t", payload={})  # another writer appends
    job.append("c", actor="t", payload={})      # stale head must be re-read
    assert EventLog(path).verify()[0]


def test_publish_plan_round_trips(backlog_dict, tmp_path):
    from vigilant_engine.intake import Backlog
    from vigilant_engine.integrations.jira import JiraPublisher, PublishOptions, PublishPlan
    data = copy.deepcopy(backlog_dict)
    plan = JiraPublisher(None, tmp_path / "m.json").prepare(
        Backlog.model_validate(data), PublishOptions(project_key="CLM", run_id="r1"))
    restored = PublishPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert restored.digest == plan.digest and restored.issues[3].depends_on
