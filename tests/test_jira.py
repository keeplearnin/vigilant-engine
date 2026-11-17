from __future__ import annotations

import json

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


def test_resume_after_failure_creates_no_duplicates(backlog_dict, tmp_path):
    backlog = Backlog.model_validate(backlog_dict)
    fake = FakeJira(FULL_HIERARCHY, fail_after=3)
    pub = JiraPublisher(client_for(fake), tmp_path / "m.json")
    with pytest.raises(JiraError):
        pub.publish(backlog, opts(), dry_run=False)
    assert len(fake.created) == 3

    fake.fail_after = None
    report = JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
        backlog, opts(), dry_run=False)
    assert len(report.skipped) == 3 and len(report.created) == 3 and len(fake.created) == 6


def test_changed_backlog_cannot_resume_partial_publish(backlog_dict, make_backlog, tmp_path):
    fake = FakeJira(FULL_HIERARCHY, fail_after=2)
    with pytest.raises(JiraError):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            Backlog.model_validate(backlog_dict), opts(), dry_run=False)
    edited = Backlog.model_validate(make_backlog(lambda d: d.update(vision="Something else")))
    with pytest.raises(PublishError, match="changed"):
        JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
            edited, opts(), dry_run=False)


def test_retries_on_429(backlog_dict, tmp_path):
    fake = FakeJira(FULL_HIERARCHY, throttle_first=True)
    JiraPublisher(client_for(fake), tmp_path / "m.json").publish(
        Backlog.model_validate(backlog_dict), opts(), dry_run=False)
    assert len(fake.created) == 6


def test_offline_preview_without_client(backlog_dict, tmp_path):
    report = JiraPublisher(None, tmp_path / "m.json").publish(
        Backlog.model_validate(backlog_dict), opts(feature_mode="label"))
    assert report.feature_mode == "label" and "Preflight skipped" in report.warnings[0]
