from __future__ import annotations

import io
import json

import httpx

from vigilant_engine.integrations.jira import JiraClient, JiraConfig
from vigilant_engine.interfaces.cli import (
    build_parser,
    cmd_import,
    cmd_plan,
    cmd_publish,
    cmd_verify,
)

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
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM", "--apply", "--yes",
         "--approver", "Test Approver"])
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


def test_import_runs_the_gate_and_feeds_publish(tmp_path, backlog_dict, make_backlog):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(backlog_dict))
    args = build_parser().parse_args(["intake", "import", "--backlog-file", str(good),
                                      "--runs-dir", str(tmp_path / "runs")])
    assert cmd_import(args, out=io.StringIO()) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "backlog.md").exists()
    pub = build_parser().parse_args(["intake", "publish", "--run", str(run_dir),
                                     "--project", "CLM"])
    assert cmd_publish(pub, client=jira_client(FakeJira(FULL_HIERARCHY)),
                       out=io.StringIO()) == 0

    bad = tmp_path / "bad.json"
    bad.write_text('{"product_name": "x",}')
    args = build_parser().parse_args(["intake", "import", "--backlog-file", str(bad),
                                      "--runs-dir", str(tmp_path / "runs2")])
    out = io.StringIO()
    assert cmd_import(args, out=out) == 1 and "not a valid backlog" in out.getvalue()


def test_yes_requires_named_approver(tmp_path, backlog_dict):
    _, run_dir, _ = run_plan(tmp_path, FakeLLM(backlog_dict))
    fake = FakeJira(FULL_HIERARCHY)
    args = build_parser().parse_args(
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM", "--apply", "--yes"])
    out = io.StringIO()
    assert cmd_publish(args, client=jira_client(fake), out=out) == 1
    assert "--approver" in out.getvalue() and fake.created == []


def test_approval_is_anchored_in_jira_and_verifiable(tmp_path, backlog_dict):
    _, run_dir, _ = run_plan(tmp_path, FakeLLM(backlog_dict))
    fake = FakeJira(FULL_HIERARCHY)
    args = build_parser().parse_args(
        ["intake", "publish", "--run", str(run_dir), "--project", "CLM", "--apply", "--yes",
         "--approver", "Hari"])
    out = io.StringIO()
    assert cmd_publish(args, client=jira_client(fake), out=out) == 0
    approval = next(json.loads(line) for line in (run_dir / "events.jsonl").read_text()
                    .splitlines() if json.loads(line)["type"] == "intake.approved")
    assert approval["payload"]["method"] == "--yes flag"
    assert all(approval["hash"] in json.dumps(i["description"]) for i in fake.created)

    verify = build_parser().parse_args(["intake", "verify", "--run", str(run_dir),
                                        "--expect-head", approval["hash"]])
    assert cmd_verify(verify, out=io.StringIO()) == 0

    # Truncate the log before the approval: the chain still "verifies" on its own,
    # but the Jira anchor exposes it.
    log = run_dir / "events.jsonl"
    lines = log.read_text().splitlines()
    cut = next(i for i, line in enumerate(lines) if '"intake.approved"' in line)
    log.write_text("\n".join(lines[:cut]) + "\n")
    assert cmd_verify(build_parser().parse_args(["intake", "verify", "--run", str(run_dir)]),
                      out=io.StringIO()) == 0
    out = io.StringIO()
    assert cmd_verify(verify, out=out) == 1 and "not in this log" in out.getvalue()


def test_plan_saves_every_attempt(tmp_path, backlog_dict, make_backlog):
    broken = make_backlog(lambda d: d["epics"][0]["features"][0]["stories"][0]
                          .update(user_story="Show status"))
    _, run_dir, _ = run_plan(tmp_path, FakeLLM(broken, backlog_dict))
    assert (run_dir / "attempts/attempt-1.json").exists()
    assert (run_dir / "attempts/attempt-2.json").exists()


def test_missing_context_file_is_explained(tmp_path):
    args = build_parser().parse_args(["intake", "plan", "--idea", "x", "--context-file",
                                      str(tmp_path / "nope.md"), "--runs-dir", str(tmp_path)])
    out = io.StringIO()
    assert cmd_plan(args, llm=FakeLLM(), out=out) == 1
    assert "was not found" in out.getvalue() and not any(tmp_path.iterdir())


def test_non_positive_limits_are_rejected():
    import pytest
    with pytest.raises(SystemExit):
        build_parser().parse_args(["intake", "plan", "--idea", "x", "--max-epics", "0"])


def test_multiple_context_files_are_combined(tmp_path, backlog_dict):
    a, b = tmp_path / "standards.md", tmp_path / "idea.md"
    a.write_text("Rule: notify the owner.")
    b.write_text("Users: 8,000 employees.")
    runs = tmp_path / "runs"
    args = build_parser().parse_args(["intake", "plan", "--idea", "x", "--context-file", str(a),
                                      "--context-file", str(b), "--runs-dir", str(runs)])
    llm = FakeLLM(backlog_dict)
    assert cmd_plan(args, llm=llm, out=io.StringIO()) == 0
    preamble = llm.requests[0]["preamble"]
    assert preamble.index("notify the owner") < preamble.index("8,000 employees")
