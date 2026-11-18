"""Small Jira Cloud REST client (API v3, Atlassian Document Format descriptions).

Credentials come from the environment and are never sent to the model:
  JIRA_BASE_URL    e.g. https://your-site.atlassian.net
  JIRA_EMAIL       the Atlassian account email
  JIRA_API_TOKEN   an API token from id.atlassian.com (Security > API tokens)

Jira Data Center / Server is not supported yet: it has no REST v3 and does not accept ADF.

Retry policy (so a retry can never create a duplicate issue):
* 429 and connection failures are retried for every request: Jira did not process them.
* Reads also retry 502/503/504 and other network errors.
* A write that fails after it may have reached Jira (5xx, timeout or dropped connection
  after sending) is never retried. It raises `JiraAmbiguousWriteError` so the caller can
  check Jira before trying again.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

READ_RETRY_STATUSES = {429, 502, 503, 504}
MAX_SEARCH_PAGES = 50
# Raised before the request reached Jira, so retrying cannot duplicate a write.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


class JiraError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Jira returned {status}: {message}" if status else message)


class JiraAmbiguousWriteError(JiraError):
    """A write failed in a way that leaves unknown whether Jira applied it."""


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
        self._base_url = config.base_url.rstrip("/")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def browse_url(self, key: str) -> str:
        return f"{self._base_url}/browse/{key}"

    # --- transport ---------------------------------------------------------------------

    def _request(
        self, method: str, path: str, json: Any | None = None, *, idempotent: bool | None = None
    ) -> Any:
        idempotent = method == "GET" if idempotent is None else idempotent
        for attempt in range(self._max_retries + 1):
            can_retry = attempt < self._max_retries
            try:
                resp = self._http.request(method, path, json=json)
            except NOT_SENT_ERRORS as exc:
                if can_retry:
                    self._backoff(attempt)
                    continue
                raise JiraError(0, f"Could not connect to Jira: {exc}") from exc
            except httpx.TransportError as exc:
                if idempotent and can_retry:
                    self._backoff(attempt)
                    continue
                if idempotent:
                    raise JiraError(0, f"Network error talking to Jira: {exc}") from exc
                raise JiraAmbiguousWriteError(
                    0, f"Network error after sending {method} {path}: {exc}"
                ) from exc

            status = resp.status_code
            if status == 429 and can_retry:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if idempotent and status in READ_RETRY_STATUSES and can_retry:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if status >= 500 and not idempotent:
                raise JiraAmbiguousWriteError(status, _error_text(resp))
            if status >= 400:
                raise JiraError(status, _error_text(resp))
            try:
                return resp.json() if resp.content else None
            except ValueError as exc:
                raise JiraError(status, f"Response was not JSON: {resp.text[:200]}") from exc
        raise JiraError(0, "Retries exhausted.")  # pragma: no cover - loop always returns/raises

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0**attempt
        self._sleep(min(delay, 60.0))

    # --- API ----------------------------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        return self._request("GET", "/rest/api/3/myself")

    def get_project(self, key: str) -> dict[str, Any]:
        return self._request("GET", f"/rest/api/3/project/{key}")

    def create_issue(self, fields: dict[str, Any]) -> str:
        data = self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        return data["key"]

    def search_issues(
        self, jql: str, fields: list[str], page_size: int = 100
    ) -> list[dict[str, Any]]:
        """All issues matching `jql`, via the enhanced search endpoint (read-only POST)."""
        issues: list[dict[str, Any]] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_SEARCH_PAGES):
            body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": page_size}
            if token:
                body["nextPageToken"] = token
            data = self._request("POST", "/rest/api/3/search/jql", json=body, idempotent=True)
            issues += data.get("issues", [])
            token = data.get("nextPageToken")
            if not token or data.get("isLast"):
                return issues
            if token in seen:
                raise JiraError(0, "Jira search returned the same page token twice; stopping.")
            seen.add(token)
        raise JiraError(0, f"Jira search exceeded {MAX_SEARCH_PAGES} pages; narrow the query.")


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    if not isinstance(body, dict):
        return str(body)[:500]
    messages = body.get("errorMessages")
    errors = body.get("errors")
    parts = [str(m) for m in messages] if isinstance(messages, list) else []
    if isinstance(errors, dict):
        parts += [f"{k}: {v}" for k, v in errors.items()]
    return "; ".join(parts) or resp.text[:500] or resp.reason_phrase
