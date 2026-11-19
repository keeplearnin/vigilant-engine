"""Run workflow shared by the CLI and the web UI: one engine, two front doors.

Both interfaces call these functions, so saving outputs, recording edits and approvals,
and judging a run by its own limits behave identically whichever one a person uses.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import anthropic
from pydantic import ValidationError

from ..evidence import EvidenceError, RunStore, digest
from .models import Backlog, IdeaBrief
from .planner import PlanResult
from .quality import Finding, Limits, has_errors, lint
from .render import to_markdown

CONTEXT_SEPARATOR = "\n\n---\n\n"
PLAN_FILE = "publish-plan.json"
MANIFEST_FILE = "jira-manifest.json"


def compose_context(parts: list[str]) -> str:
    """Join context pieces (standards first, then the idea's own context)."""
    return CONTEXT_SEPARATOR.join(p.strip() for p in parts if p and p.strip())


def save_plan_outputs(store: RunStore, brief: IdeaBrief, result: PlanResult) -> str:
    """Write the review files for a finished plan and record it. Returns the backlog digest."""
    store.write_json("brief.json", brief.model_dump())
    store.write_json("backlog.json", result.backlog.model_dump())
    store.write_json("lint.json", [f.to_dict() for f in result.findings])
    store.write_json("model-calls.json", result.calls)
    store.write_text("backlog.md", to_markdown(result.backlog, result.findings, store.run_id))
    backlog_digest = digest(result.backlog.model_dump())
    store.events.append("intake.planned", actor="agent:intake", payload={
        "backlog_digest": backlog_digest,
        "counts": result.backlog.counts(),
        "attempts": result.attempts,
        "chosen_attempt": result.chosen_attempt,
        "repair_error": result.repair_error,
        "publishable": result.publishable,
    })
    return backlog_digest


def save_limits(store: RunStore, limits: Limits) -> None:
    store.write_json("limits.json", asdict(limits))


def load_limits(store: RunStore) -> Limits:
    """The limits the run was planned with, so it is always judged by the same rules."""
    path = store.path("limits.json")
    if not path.exists():
        return Limits()
    data = json.loads(path.read_text(encoding="utf-8"))
    return Limits(**{k: int(v) for k, v in data.items() if k in Limits.__dataclass_fields__})


def load_backlog(store: RunStore) -> tuple[Backlog, Limits, list[Finding]]:
    """Load and gate a run's backlog. Raises FileNotFoundError, ValidationError or
    json.JSONDecodeError for the caller to explain."""
    backlog = Backlog.model_validate_json(store.path("backlog.json").read_text(encoding="utf-8"))
    limits = load_limits(store)
    return backlog, limits, lint(backlog, limits)


def save_backlog(store: RunStore, backlog: Backlog) -> list[Finding]:
    """Save an edited backlog and refresh the review files. Returns the new findings."""
    limits = load_limits(store)
    findings = lint(backlog, limits)
    store.write_json("backlog.json", backlog.model_dump())
    store.write_json("lint.json", [f.to_dict() for f in findings])
    store.write_text("backlog.md", to_markdown(backlog, findings, store.run_id))
    return findings


def planned_digest(store: RunStore) -> str | None:
    found = None
    for event in store.events.events():
        if event.get("type") == "intake.planned":
            found = event.get("payload", {}).get("backlog_digest")
    return found


def record_edit_if_changed(store: RunStore, backlog: Backlog, actor: str) -> str:
    """Record a human edit when the backlog differs from what was planned. Returns its digest."""
    current = digest(backlog.model_dump())
    planned = planned_digest(store)
    if planned and planned != current:
        last_edit = None
        for event in store.events.events():
            if event.get("type") == "intake.backlog_edited":
                last_edit = event.get("payload", {}).get("to")
        if last_edit != current:  # one event per distinct edit, not per command
            store.events.append("intake.backlog_edited", actor=actor,
                                payload={"from": planned, "to": current})
    return current


def record_approval(store: RunStore, *, plan_digest: str, project: str, issues: int,
                    approver: str, method: str, os_user: str, interactive: bool,
                    backlog_digest: str) -> dict[str, Any]:
    """Record a human approval. Local identity is self-asserted, so the method is recorded
    with it; a hosted deployment takes the approver from single sign-on instead."""
    return store.events.append("intake.approved", actor=f"human:{approver}", payload={
        "project": project, "backlog_digest": backlog_digest, "plan_digest": plan_digest,
        "issues": issues, "method": method, "os_user": os_user, "interactive": interactive,
    })


def validation_summary(exc: Exception, limit: int = 10) -> str:
    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors()[:limit]:
            where = ".".join(str(p) for p in err["loc"]) or "backlog"
            lines.append(f"  {where}: {err['msg']}")
        more = len(exc.errors()) - limit
        if more > 0:
            lines.append(f"  ... and {more} more")
        return "\n".join(lines)
    if isinstance(exc, json.JSONDecodeError):
        return f"  line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return f"  {exc}"


def run_summary(store: RunStore) -> dict[str, Any]:
    """A plain-language status for one run, derived only from its files and event log."""
    summary: dict[str, Any] = {"run_id": store.run_id, "product": None, "counts": None,
                               "state": "in progress", "error": None, "created": [],
                               "started": None, "errors": 0, "warnings": 0}
    try:
        events = list(store.events.events())
    except EvidenceError as exc:
        summary.update(state="damaged", error=str(exc))
        return summary
    if events:
        summary["started"] = events[0].get("ts")
    failures = [e for e in events if e.get("type") == "run.failed"]

    if store.path("backlog.json").exists():
        try:
            backlog, _, findings = load_backlog(store)
        except (ValidationError, json.JSONDecodeError) as exc:
            summary.update(state="invalid backlog", error=validation_summary(exc, 3))
            return summary
        summary.update(product=backlog.product_name, counts=backlog.counts(),
                       errors=sum(f.severity == "error" for f in findings),
                       warnings=sum(f.severity == "warning" for f in findings))
        summary["state"] = "blocked" if has_errors(findings) else "ready to publish"
        manifest_path = store.path(MANIFEST_FILE)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            created = manifest.get("created", {})
            summary["created"] = sorted(created.values(),
                                        key=lambda k: int(k.rsplit("-", 1)[-1]))
            planned_total = None
            if store.path(PLAN_FILE).exists():
                planned_total = len(store.read_json(PLAN_FILE)["issues"])
            if created:
                done = planned_total is not None and len(created) >= planned_total
                summary["state"] = "published" if done else "partly published"
            if manifest.get("uncertain"):
                summary["state"] = "needs check"
    elif store.path("questions.json").exists():
        summary["state"] = "waiting for answers"
    elif failures:
        summary.update(state="failed", error=failures[-1].get("payload", {}).get("error"))
    return summary


def claude_error_message(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude rejected the API credentials. Check ANTHROPIC_API_KEY or `ant auth login`."
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude rate limit reached. Wait a minute and run plan again."
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return f"Claude is temporarily unavailable ({exc.status_code}). Try again shortly."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not connect to Claude. Check the network and try again."
    return str(exc)
