"""Small Jira Cloud REST client (API v3, Atlassian Document Format descriptions).

Credentials come from the environment and are never sent to the model:
  JIRA_BASE_URL    e.g. https://your-site.atlassian.net
  JIRA_EMAIL       the Atlassian account email
  JIRA_API_TOKEN   an API token from id.atlassian.com (Security > API tokens)

Jira Data Center / Server is not supported yet: it has no REST v3 and does not accept ADF.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

RETRY_STATUSES = {429, 502, 503, 504}


class JiraError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Jira returned {status}: {message}" if status else message)


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    email: str | None = None
    api_token: str | None = None

    @classmethod
    def from_env(cls) -> JiraConfig:
        base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        cfg = cls(
            base_url=base,
            email=os.environ.get("JIRA_EMAIL"),
            api_token=os.environ.get("JIRA_API_TOKEN"),
        )
        if not base:
            raise JiraError(0, "Set JIRA_BASE_URL.")
        if not (cfg.email and cfg.api_token):
            raise JiraError(0, "Set JIRA_EMAIL and JIRA_API_TOKEN.")
        return cfg

    def auth(self) -> httpx.Auth:
        return httpx.BasicAuth(self.email or "", self.api_token or "")

    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Content-Type": "application/json"}


class JiraClient:
    def __init__(
        self,
        config: JiraConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._http = httpx.Client(
            base_url=config.base_url,
            auth=config.auth(),
            headers=config.headers(),
            timeout=30.0,
            transport=transport,
        )
        self._max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, json: Any | None = None) -> Any:
        for attempt in range(self._max_retries + 1):
            resp = self._http.request(method, path, json=json)
            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                self._sleep(min(delay, 60.0))
                continue
            if resp.status_code >= 400:
                raise JiraError(resp.status_code, _error_text(resp))
            return resp.json() if resp.content else None
        raise JiraError(0, "retries exhausted")  # pragma: no cover

    def myself(self) -> dict[str, Any]:
        return self._request("GET", "/rest/api/3/myself")

    def get_project(self, key: str) -> dict[str, Any]:
        return self._request("GET", f"/rest/api/3/project/{key}")

    def create_issue(self, fields: dict[str, Any]) -> str:
        data = self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        return data["key"]


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    parts = list(body.get("errorMessages", []))
    parts += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
    return "; ".join(parts) or resp.text[:500]
