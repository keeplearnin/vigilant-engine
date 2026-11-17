from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vigilant_engine.evidence import EventLog
from vigilant_engine.intake import Backlog
from vigilant_engine.integrations.jira import (
    JiraClient,
    JiraConfig,
    JiraError,
    JiraPublisher,
    PublishError,
    PublishOptions,
)

from .conftest import DEFAULT_HIERARCHY, FULL_HIERARCHY, FakeJira


def client_for(fake: FakeJira) -> JiraClient:
    cfg = JiraConfig(base_url="https://example.atlassian.net", email="a@b.c", api_token="t")
    return JiraClient(cfg, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)


def opts(**kw) -> PublishOptions:
    return PublishOptions(project_key="CLM", run_id="run-1", **kw)


def test_dry_run_writes_nothing(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY)
    pub = JiraPublisher(client_for(fake), tmp_path / "m.json")
    report = pub.publish(Backlog.model_validate(backlog_dict), opts())
    assert report.feature_mode == "issue-type" and len(report.planned) == 6
    assert fake.created == [] and not (tmp_path / "m.json").exists()
    assert all(r.method == "GET" for r in fake.requests)


def test_creates_epic_feature_story_with_parents(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY)
    events = EventLog(tmp_path / "events.jsonl")
    pub = JiraPublisher(client_for(fake), tmp_path / "m.json", events=events)
    report = pub.publish(Backlog.model_validate(backlog_dict),
                         opts(story_points_field="customfield_10016"), dry_run=False)

    by_summary = {i["summary"]: i for i in fake.created}
    epic = by_summary["Self-service claim status"]
    feature = by_summary["Claim status page"]
    story = by_summary["Story story-missing-docs"]
    assert epic["issuetype"] == {"name": "Epic"} and "parent" not in epic
    assert feature["issuetype"] == {"name": "Feature"} and feature["parent"] == {"key": epic["key"]}
    assert story["parent"] == {"key": feature["key"]}
    assert story["customfield_10016"] == 5
    assert {"ai-generated", "vigilant-run-1", "moscow-must", "kind-user"} <= set(story["labels"])
    assert story["description"]["type"] == "doc"
    assert "S1.1.1 Story story-view-status" in json.dumps(story["description"])
    assert len(report.created) == 6
    assert json.loads((tmp_path / "m.json").read_text())["created"]["S1.1.2"] == story["key"]
    assert events.verify()[0]


def test_label_mode_when_project_has_no_feature_type(backlog_dict, tmp_path):
    fake = FakeJira(DEFAULT_HIERARCHY)
    pub = JiraPublisher(client_for(fake), tmp_path / "m.json")
    report = pub.publish(Backlog.model_validate(backlog_dict), opts(), dry_run=False)
    assert report.feature_mode == "label" and report.warnings
    epic_key = fake.created[0]["key"]
    stories = [i for i in fake.created if i["issuetype"] == {"name": "Story"}]
    assert len(fake.created) == 4 and all(s["parent"] == {"key": epic_key} for s in stories)
    assert "feature-feat-alerts" in stories[-1]["labels"]


def test_explicit_issue_type_mode_fails_without_feature(backlog_dict, tmp_path):
    pub = JiraPublisher(client_for(FakeJira(DEFAULT_HIERARCHY)), tmp_path / "m.json")
    with pytest.raises(PublishError, match="does not exist"):
        pub.publish(Backlog.model_validate(backlog_dict), opts(feature_mode="issue-type"))


def test_missing_story_type_blocks_before_writing(backlog_dict, tmp_path):
    fake = FakeJira([{"name": "Epic", "hierarchyLevel": 1}, {"name": "Task", "hierarchyLevel": 0}])
    with pytest.raises(PublishError, match="Story"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            Backlog.model_validate(backlog_dict), opts(), dry_run=False)
    assert fake.created == []


def test_issue_cap(backlog_dict, tmp_path):
    pub = JiraPublisher(client_for(FakeJira(FULL_HIERARCHY)), tmp_path / "m.json")
    with pytest.raises(PublishError, match="cap"):
        pub.publish(Backlog.model_validate(backlog_dict), opts(max_issues=3), dry_run=False)


def test_definite_failure_resumes_without_duplicates(backlog_dict, tmp_path):
    backlog = Backlog.model_validate(backlog_dict)
    fake = FakeJira(FULL_HIERARCHY, post_script=["ok", "ok", "ok", "400"])
    with pytest.raises(JiraError, match="400"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            backlog, opts(), dry_run=False)
    assert len(fake.created) == 3

    report = JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
        backlog, opts(), dry_run=False)
    assert len(report.skipped) == 3 and len(report.created) == 3 and len(fake.created) == 6


def test_changed_backlog_cannot_resume_partial_publish(backlog_dict, make_backlog, tmp_path):
    fake = FakeJira(FULL_HIERARCHY, post_script=["ok", "ok", "400"])
    with pytest.raises(JiraError):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            Backlog.model_validate(backlog_dict), opts(), dry_run=False)
    edited = Backlog.model_validate(make_backlog(lambda d: d.update(vision="Something else")))
    with pytest.raises(PublishError, match="changed"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            edited, opts(), dry_run=False)


def test_retries_on_429(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY, post_script=["429", "429"])
    JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
        Backlog.model_validate(backlog_dict), opts(), dry_run=False)
    assert len(fake.created) == 6


# --- ambiguous writes are never retried blindly ------------------------------------------

class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def publisher(fake, tmp_path, clock):
    return JiraPublisher(client_for(fake), tmp_path / "m.json", clock=clock)


@pytest.mark.parametrize("failure", ["commit_504", "commit_reset"])
def test_ambiguous_write_that_committed_is_adopted_not_duplicated(
        backlog_dict, tmp_path, failure):
    backlog, clock = Backlog.model_validate(backlog_dict), Clock()
    fake = FakeJira(FULL_HIERARCHY, post_script=["ok", "ok", failure])
    with pytest.raises(PublishError, match="may or may not"):
        publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)
    assert len(fake.created) == 3  # the third POST was sent once, never retried
    manifest = json.loads((tmp_path / "m.json").read_text())
    assert list(manifest["uncertain"]) == ["S1.1.1"]

    clock.advance(61)
    report = publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)
    assert report.reconciled == {"S1.1.1": "CLM-3"}
    assert len(fake.created) == 6  # no duplicate
    assert json.loads((tmp_path / "m.json").read_text())["uncertain"] == {}


def test_ambiguous_write_that_did_not_commit_is_created_on_resume(backlog_dict, tmp_path):
    backlog, clock = Backlog.model_validate(backlog_dict), Clock()
    fake = FakeJira(FULL_HIERARCHY, post_script=["ok", "504"])
    with pytest.raises(PublishError):
        publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)
    assert len(fake.created) == 1

    clock.advance(61)
    report = publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)
    assert report.reconciled == {} and len(fake.created) == 6


def test_resume_waits_for_search_index(backlog_dict, tmp_path):
    backlog, clock = Backlog.model_validate(backlog_dict), Clock()
    fake = FakeJira(FULL_HIERARCHY, post_script=["commit_504"])
    with pytest.raises(PublishError):
        publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)
    clock.advance(10)
    with pytest.raises(PublishError, match="Wait"):
        publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)
    assert len(fake.created) == 1


def test_several_matches_stop_for_manual_cleanup(backlog_dict, tmp_path):
    backlog, clock = Backlog.model_validate(backlog_dict), Clock()
    fake = FakeJira(FULL_HIERARCHY, post_script=["commit_504"])
    with pytest.raises(PublishError):
        publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)
    fake.created.append({**fake.created[0], "key": "CLM-99"})  # someone retried by hand
    clock.advance(61)
    with pytest.raises(PublishError, match="several issues"):
        publisher(fake, tmp_path, clock).publish(backlog, opts(), dry_run=False)


def test_connection_refused_is_safe_to_retry(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY, post_script=["connect_error"])
    JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
        Backlog.model_validate(backlog_dict), opts(), dry_run=False)
    assert len(fake.created) == 6


def test_reads_retry_on_503_and_timeouts(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY, get_script=["503", "read_timeout"])
    report = JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
        Backlog.model_validate(backlog_dict), opts())
    assert len(report.planned) == 6


def test_read_network_errors_become_jira_errors(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY, get_script=["read_timeout"] * 5)
    with pytest.raises(JiraError, match="Network error"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            Backlog.model_validate(backlog_dict), opts())


@pytest.mark.parametrize("body", [b"null", b'["bad request"]', b'"oops"', b"<html>x</html>"])
def test_odd_error_bodies_still_raise_jira_error(body):
    def handler(request):
        return httpx.Response(400, content=body)
    cfg = JiraConfig(base_url="https://example.atlassian.net", email="a@b.c", api_token="t")
    client = JiraClient(cfg, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(JiraError, match="400"):
        client.get_project("CLM")


# --- the approved plan is the executed plan; resume keeps the same shape -------------------

def test_execute_runs_the_approved_plan_without_replanning(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY)
    pub = JiraPublisher(client_for(fake), tmp_path / "m.json")
    plan = pub.prepare(Backlog.model_validate(backlog_dict), opts())
    gets_before = sum(r.method == "GET" for r in fake.requests)
    fake.issue_types = DEFAULT_HIERARCHY  # project changes after approval
    pub.execute(plan)
    assert sum(r.method == "GET" for r in fake.requests) == gets_before
    assert [i["issuetype"]["name"] for i in fake.created].count("Feature") == 2


def test_resume_keeps_feature_mode(backlog_dict, tmp_path):
    backlog = Backlog.model_validate(backlog_dict)
    fake = FakeJira(FULL_HIERARCHY, post_script=["ok", "ok", "400"])
    with pytest.raises(JiraError):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            backlog, opts(), dry_run=False)

    with pytest.raises(PublishError, match="issue-type mode"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            backlog, opts(feature_mode="label"), dry_run=False)
    with pytest.raises(PublishError, match="issue types"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            backlog, opts(story_type="Task"), dry_run=False)

    fake.issue_types = DEFAULT_HIERARCHY  # Feature type removed: auto must not switch modes
    with pytest.raises(PublishError, match="does not exist"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            backlog, opts(), dry_run=False)

    fake.issue_types = FULL_HIERARCHY
    report = JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
        backlog, opts(), dry_run=False)
    assert report.feature_mode == "issue-type" and len(fake.created) == 6


def test_offline_preview_without_client(backlog_dict, tmp_path):
    report = JiraPublisher(None, tmp_path / "m.json").publish(
        Backlog.model_validate(backlog_dict), opts(feature_mode="label"))
    assert report.feature_mode == "label" and "Preflight skipped" in report.warnings[0]
