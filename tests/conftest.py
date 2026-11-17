from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest

from vigilant_engine.llm import LLMResult


def _story(ref: str, depends_on: list[str] | None = None, points: int = 3) -> dict[str, Any]:
    return {
        "ref": ref,
        "title": f"Story {ref}",
        "kind": "user",
        "user_story": "As a policyholder, I want to see my claim status, so that I know what "
                      "happens next.",
        "description": "Show the current claim stage and the next expected step.",
        "acceptance_criteria": [
            {"given": "an open claim", "when": "I open the status page",
             "then": "I see the current stage"},
            {"given": "a claim waiting on documents", "when": "I open the status page",
             "then": "I see which documents are missing"},
        ],
        "story_points": points,
        "priority": "must",
        "depends_on": depends_on or [],
        "labels": ["claims"],
    }


def sample_backlog() -> dict[str, Any]:
    return {
        "product_name": "Claim Status Tracker",
        "problem_statement": "Policyholders call the contact centre to ask where their claim is.",
        "vision": "Every policyholder can see their claim progress without calling.",
        "target_users": ["Policyholders", "Claims handlers"],
        "epics": [
            {
                "ref": "epic-status",
                "title": "Self-service claim status",
                "summary": "Let policyholders track claims online.",
                "business_outcome": "Fewer status calls to the contact centre.",
                "success_metrics": ["Status calls down 30% in 6 months"],
                "priority": "must",
                "in_mvp": True,
                "features": [
                    {
                        "ref": "feat-status-page",
                        "title": "Claim status page",
                        "description": "A page showing claim progress.",
                        "benefit_hypothesis": "Visible progress reduces anxiety and calls.",
                        "priority": "must",
                        "stories": [_story("story-view-status"),
                                    _story("story-missing-docs", ["story-view-status"], 5)],
                    },
                    {
                        "ref": "feat-alerts",
                        "title": "Status change alerts",
                        "description": "Notify policyholders when the stage changes.",
                        "benefit_hypothesis": "Proactive updates prevent calls.",
                        "priority": "should",
                        "stories": [_story("story-sms-optin", ["story-view-status"], 2)],
                    },
                ],
            }
        ],
        "assumptions": ["Claims system exposes a status API"],
        "open_questions": ["Which channels besides SMS?"],
        "out_of_scope": ["Filing new claims"],
        "risks": ["Status data quality in legacy claims system"],
    }


@pytest.fixture
def backlog_dict() -> dict[str, Any]:
    return sample_backlog()


@pytest.fixture
def make_backlog(backlog_dict):
    def _make(mutate=None) -> dict[str, Any]:
        data = copy.deepcopy(backlog_dict)
        if mutate:
            mutate(data)
        return data
    return _make


class FakeLLM:
    """Returns queued JSON answers and records every request."""

    model = "claude-opus-5-5"

    def __init__(self, *answers: dict[str, Any] | Exception):
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def structured(self, **kwargs) -> LLMResult:
        self.requests.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(text=json.dumps(answer), model=self.model, stop_reason="end_turn",
                         usage={"input_tokens": 10, "output_tokens": 20})


class FakeJira:
    """In-memory Jira Cloud REST API v3 behind httpx.MockTransport."""

    def __init__(self, issue_types: list[dict[str, Any]], fail_after: int | None = None,
                 throttle_first: bool = False):
        self.issue_types = issue_types
        self.created: list[dict[str, Any]] = []
        self.fail_after = fail_after
        self.throttle_first = throttle_first
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "abc"})
        if path.startswith("/rest/api/3/project/"):
            return httpx.Response(200, json={"key": "CLM", "issueTypes": self.issue_types})
        if path == "/rest/api/3/issue" and request.method == "POST":
            if self.throttle_first:
                self.throttle_first = False
                return httpx.Response(429, headers={"Retry-After": "1"})
            if self.fail_after is not None and len(self.created) >= self.fail_after:
                return httpx.Response(500, json={"errorMessages": ["boom"]})
            fields = json.loads(request.content)["fields"]
            key = f"CLM-{len(self.created) + 1}"
            self.created.append({"key": key, **fields})
            return httpx.Response(201, json={"key": key})
        return httpx.Response(404, json={"errorMessages": [f"no route {path}"]})


FULL_HIERARCHY = [
    {"id": "1", "name": "Epic", "subtask": False, "hierarchyLevel": 2},
    {"id": "2", "name": "Feature", "subtask": False, "hierarchyLevel": 1},
    {"id": "3", "name": "Story", "subtask": False, "hierarchyLevel": 0},
    {"id": "4", "name": "Sub-task", "subtask": True, "hierarchyLevel": -1},
]
DEFAULT_HIERARCHY = [
    {"id": "1", "name": "Epic", "subtask": False, "hierarchyLevel": 1},
    {"id": "3", "name": "Story", "subtask": False, "hierarchyLevel": 0},
    {"id": "5", "name": "Task", "subtask": False, "hierarchyLevel": 0},
]
