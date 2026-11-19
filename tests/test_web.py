"""Local web UI: full flow, editing, governance and local-only safety."""

from __future__ import annotations

import copy
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from vigilant_engine.integrations.jira import JiraClient, JiraConfig
from vigilant_engine.interfaces.web.app import WebConfig, create_app
from vigilant_engine.llm import LLMError

from .conftest import FULL_HIERARCHY, FakeJira, FakeLLM

QUESTIONS = {"questions": [{"question": "Which users?", "why_it_matters": "Scope",
                            "default_assumption": "Policyholders"}]}


class Harness:
    def __init__(self, tmp_path, answers, jira: FakeJira | None = None):
        self.llm = FakeLLM(*answers)
        self.jira = jira or FakeJira(FULL_HIERARCHY)
        standards = tmp_path / "standards"
        standards.mkdir()
        (standards / "security-identity.md").write_text("# Standards: identity\n\nNotify owners.")
        (standards / "TEMPLATE.md").write_text("# template")
        config = WebConfig(
            runs_dir=tmp_path / "runs", standards_dir=standards, inline_jobs=True,
            llm_factory=lambda: self.llm, jira_factory=self._jira,
            jira_base_url="https://example.atlassian.net", allowed_hosts={"testserver"})
        self.client = TestClient(create_app(config))
        self.runs = tmp_path / "runs"

    def _jira(self):
        cfg = JiraConfig(base_url="https://example.atlassian.net", email="a@b.c", api_token="t")
        return JiraClient(cfg, transport=httpx.MockTransport(self.jira.handler),
                          sleep=lambda s: None)

    def csrf(self, path="/new") -> str:
        return re.search(r'name="csrf" value="([^"]+)"', self.client.get(path).text).group(1)

    def post(self, path, data, follow=True):
        return self.client.post(path, data={"csrf": self.csrf(), **data},
                                follow_redirects=follow)

    def start(self, idea="Let customers track claims", **extra):
        response = self.post("/new", {"idea": idea, **extra}, follow=False)
        assert response.status_code == 303, response.text
        return response.headers["location"].rsplit("/", 1)[-1]


# --- safety -----------------------------------------------------------------------------------

def test_rejects_foreign_host_missing_token_and_cross_site(tmp_path):
    h = Harness(tmp_path, [])
    assert h.client.get("/", headers={"host": "evil.example"}).status_code == 403
    assert h.client.post("/new", data={"idea": "x"}).status_code == 403
    response = h.client.post("/new", data={"idea": "x", "csrf": h.csrf()},
                             headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert "frame-ancestors 'none'" in h.client.get("/").headers["content-security-policy"]


def test_model_output_is_escaped(tmp_path, backlog_dict):
    data = copy.deepcopy(backlog_dict)
    data["product_name"] = "<script>alert(1)</script>"
    h = Harness(tmp_path, [data])
    run_id = h.start()
    page = h.client.get(f"/runs/{run_id}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_unknown_or_malformed_run_ids_are_404(tmp_path):
    h = Harness(tmp_path, [])
    assert h.client.get("/runs/..%2F..%2Fetc").status_code == 404
    assert h.client.get("/runs/20260101T000000Z-abcdef").status_code == 404


# --- full flow ------------------------------------------------------------------------------

def test_idea_to_jira_through_the_ui(tmp_path, backlog_dict):
    h = Harness(tmp_path, [QUESTIONS, backlog_dict])
    assert "No runs yet" in h.client.get("/").text

    run_id = h.start(clarify="on", standards="security-identity", users="Policyholders")
    page = h.client.get(f"/runs/{run_id}").text
    assert "A few questions first" in page and "Which users?" in page
    assert "Notify owners." in h.llm.requests[0]["preamble"]   # standards sent as context
    assert "Policyholders" in h.llm.requests[0]["preamble"]

    page = h.post(f"/runs/{run_id}/answers", {"answer_0": ""}).text
    assert "Quality gate passed" in page and "Self-service claim status" in page
    assert "(assumed) Policyholders" in h.llm.requests[1]["preamble"]

    page = h.post(f"/runs/{run_id}/preview", {"project": "clm"}).text
    assert "Plan for CLM" in page and "Approve and create 6 issues" in page
    digest = re.search(r'name="plan_digest" value="([^"]+)"', page).group(1)

    page = h.post(f"/runs/{run_id}/approve", {"plan_digest": digest, "approver": "Hari",
                                              "confirm_project": "WRONG"}).text
    assert "Not approved" in page and h.jira.created == []

    page = h.post(f"/runs/{run_id}/approve", {"plan_digest": digest, "approver": "Hari",
                                              "confirm_project": "CLM"}).text
    assert len(h.jira.created) == 6 and "published" in page
    assert "https://example.atlassian.net/browse/CLM-1" in page
    approval = re.search(r'(sha256:[0-9a-f]{64})</span>', page).group(1)
    assert all(approval in json.dumps(i["description"]) for i in h.jira.created)

    page = h.post(f"/runs/{run_id}/verify", {"expect_head": approval}).text
    assert "Audit check passed" in page
    assert "published" in h.client.get("/").text


def test_editing_a_story_reruns_gate_and_invalidates_preview(tmp_path, backlog_dict):
    h = Harness(tmp_path, [backlog_dict])
    run_id = h.start()
    page = h.post(f"/runs/{run_id}/preview", {"project": "CLM"}).text
    digest = re.search(r'name="plan_digest" value="([^"]+)"', page).group(1)

    page = h.post(f"/runs/{run_id}/stories/story-view-status", {
        "title": "View claim status", "user_story": "Show status", "description": "",
        "story_points": "3", "priority": "must",
        "criteria": "Given a claim | When I look | Then I see it\nGiven x | When y | Then z"}).text
    assert "Blocked" in page and "user-story-format" in page   # gate ran again
    assert "Fix the quality-gate errors above first" in page

    page = h.post(f"/runs/{run_id}/approve", {"plan_digest": digest, "approver": "Hari",
                                              "confirm_project": "CLM"}).text
    assert "Not approved" in page or "Preview the Jira plan first" in page
    assert h.jira.created == []


def test_bad_criteria_are_explained_not_saved(tmp_path, backlog_dict):
    h = Harness(tmp_path, [backlog_dict])
    run_id = h.start()
    page = h.post(f"/runs/{run_id}/stories/story-view-status", {
        "title": "t", "user_story": "As a user, I want x, so that y", "description": "",
        "story_points": "3", "priority": "must", "criteria": "just one part"}).text
    assert "Not saved" in page and "three parts" in page


def test_json_editor_validates(tmp_path, backlog_dict):
    h = Harness(tmp_path, [backlog_dict])
    run_id = h.start()
    page = h.post(f"/runs/{run_id}/json", {"backlog": '{"product_name": "x",}'}).text
    assert "Not saved" in page
    data = copy.deepcopy(backlog_dict)
    data["vision"] = "Edited in the browser"
    page = h.post(f"/runs/{run_id}/json", {"backlog": json.dumps(data)}).text
    assert "Backlog saved" in page and "Edited in the browser" in page


def test_planning_failure_offers_retry(tmp_path):
    h = Harness(tmp_path, [LLMError("hit max_tokens")])
    run_id = h.start()
    page = h.client.get(f"/runs/{run_id}").text
    assert "Planning stopped" in page and f"/new?source={run_id}" in page
    retry = h.client.get(f"/new?source={run_id}").text
    assert "Let customers track claims" in retry   # form refilled from the failed run


def test_status_endpoint_and_standards_page(tmp_path, backlog_dict):
    h = Harness(tmp_path, [backlog_dict])
    run_id = h.start()
    status = h.client.get(f"/runs/{run_id}/status").json()
    assert status["working"] is False and status["state"] == "ready to publish"
    page = h.client.get("/standards").text
    assert "Identity" in page and "TEMPLATE" not in page


def test_missing_claude_key_is_explained(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "standards").mkdir()
    app = create_app(WebConfig(runs_dir=tmp_path / "runs2", standards_dir=tmp_path / "standards",
                               inline_jobs=True, allowed_hosts={"testserver"}))
    client = TestClient(app)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/new").text).group(1)
    page = client.post("/new", data={"csrf": token, "idea": "x"}).text
    assert "No Claude API key found" in page
    assert not any((tmp_path / "runs2").iterdir())


@pytest.mark.parametrize("bad", ["0", "-3", "lots"])
def test_bad_limits_are_explained(tmp_path, bad):
    h = Harness(tmp_path, [])
    page = h.post("/new", {"idea": "x", "max_epics": bad}).text
    assert "whole numbers of 1 or more" in page
