from __future__ import annotations

import io
import json

import httpx

from vigilant_engine.integrations.jira import JiraClient, JiraConfig
from vigilant_engine.interfaces.cli import build_parser, cmd_plan, cmd_publish, cmd_verify

from .conftest import FULL_HIERARCHY, FakeJira, FakeLLM


def run_plan(tmp_path, llm, *extra, stdin=""):
    args = build_parser().parse_args(
        ["intake", "plan", "--idea", "Let customers track claims", "--runs-dir",
         str(tmp_path), *extra])
    out = io.StringIO()
    code = cmd_plan(args, llm=llm, stdin=io.StringIO(stdin), out=out)
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    return code, run_dir, out.getvalue()


def jira_client(fake):
    cfg = JiraConfig(base_url="https://example.atlassian.net", email="a@b.c", api_token="t")
    return JiraClient(cfg, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)


def test_plan_then_publish_end_to_end(tmp_path, backlog_dict):
    questions = {"questions": [{"question": "Which channels?", "why_it_matters": "Scope",
                                "default_assumption": "SMS only"}]}
    code, run_dir, out = run_plan(tmp_path, FakeLLM(questions, backlog_dict), "--interactive",
                                  stdin="\n")
    assert code == 0, out
    for name in ("idea.md", "brief.json", "backlog.json", "backlog.md", "lint.json"):
        assert (run_dir / name).exists()
    brief = json.loads((run_dir / "brief.json").read_text())
    assert brief["answers"] == [["Which channels?", "(assumed) SMS only"]]

    fake = FakeJira(FULL_HIERARCHY)
    pub_args = build_parser().parse_args(
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM"])
    out = io.StringIO()
    assert cmd_publish(pub_args, client=jira_client(fake), out=out) == 0
    assert "Dry run" in out.getvalue() and fake.created == []

    pub_args = build_parser().parse_args(
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM", "--apply"])
    out = io.StringIO()
    code = cmd_publish(pub_args, client=jira_client(fake), stdin=io.StringIO("CLM\n"), out=out)
    assert code == 0, out.getvalue()
    assert len(fake.created) == 6

    events = [json.loads(line)["type"] for line in
              (run_dir / "events.jsonl").read_text().splitlines()]
    assert "intake.approved" in events and events.count("jira.issue_created") == 6
    verify_args = build_parser().parse_args(["intake", "verify", "--run", str(run_dir)])
    assert cmd_verify(verify_args, out=io.StringIO()) == 0


def test_publish_needs_typed_confirmation(tmp_path, backlog_dict):
    _, run_dir, _ = run_plan(tmp_path, FakeLLM(backlog_dict))
    fake = FakeJira(FULL_HIERARCHY)
    args = build_parser().parse_args(
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM", "--apply"])
    assert cmd_publish(args, client=jira_client(fake), stdin=io.StringIO("yes\n"),
                       out=io.StringIO()) == 2
    assert fake.created == []


def test_blocked_backlog_is_not_published(tmp_path, make_backlog):
    broken = make_backlog(lambda d: d["epics"][0]["features"][0].update(stories=[]))
    code, run_dir, out = run_plan(tmp_path, FakeLLM(broken, broken, broken))
    assert code == 2 and "Blocked" in out
    fake = FakeJira(FULL_HIERARCHY)
    args = build_parser().parse_args(
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM", "--apply", "--yes"])
    assert cmd_publish(args, client=jira_client(fake), out=io.StringIO()) == 2
    assert fake.created == []


def test_human_edit_is_recorded(tmp_path, backlog_dict):
    _, run_dir, _ = run_plan(tmp_path, FakeLLM(backlog_dict))
    data = json.loads((run_dir / "backlog.json").read_text())
    data["vision"] = "Edited by the product owner"
    (run_dir / "backlog.json").write_text(json.dumps(data))
    args = build_parser().parse_args(
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM"])
    cmd_publish(args, client=jira_client(FakeJira(FULL_HIERARCHY)), out=io.StringIO())
    assert "intake.backlog_edited" in (run_dir / "events.jsonl").read_text()


def test_tampered_event_log_fails_verification(tmp_path, backlog_dict):
    _, run_dir, _ = run_plan(tmp_path, FakeLLM(backlog_dict))
    log = run_dir / "events.jsonl"
    log.write_text(log.read_text().replace("agent:intake", "agent:someone-else", 1))
    args = build_parser().parse_args(["intake", "verify", "--run", str(run_dir)])
    assert cmd_verify(args, out=io.StringIO()) == 1
