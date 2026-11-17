"""Minimal evidence plane: a run folder plus an append-only, hash-chained event log.

Each event commits to the hash of the previous one, so any edit to the history is
detectable with `EventLog.verify()` (invariant I8). This is the local-first form of the
evidence plane described in docs/architecture/ARCHITECTURE.md.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "sha256:" + "0" * 64


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(obj: Any) -> str:
    data = obj if isinstance(obj, bytes) else canonical(obj)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


class EvidenceError(RuntimeError):
    """The event log is damaged or was edited; nothing more may be appended to it."""


class EventLog:
    """Append-only, hash-chained JSONL log. Assumes one writer at a time per run."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head: tuple[int, str] | None = None  # (seq, hash) of the last event

    def events(self):
        """Yield every event. Raises EvidenceError on a line that is not a JSON object."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EvidenceError(f"Event {n}: not valid JSON ({exc.msg}).") from exc
                if not isinstance(event, dict):
                    raise EvidenceError(f"Event {n}: not a JSON object.")
                yield event

    def append(self, event_type: str, *, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._head is None:
            ok, message = self.verify()
            if not ok and self.path.exists():
                raise EvidenceError(
                    f"Refusing to extend a damaged event log ({self.path}): {message}"
                )
            self._head = self._scan_head()
        seq, prev = self._head
        event = {
            "seq": seq + 1,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "type": event_type,
            "actor": actor,
            "payload": payload,
            "prev_hash": prev,
        }
        event["hash"] = digest(event)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._head = (event["seq"], event["hash"])
        return event

    def _scan_head(self) -> tuple[int, str]:
        head = (0, GENESIS)
        for event in self.events():
            head = (event["seq"], event["hash"])
        return head

    def verify(self) -> tuple[bool, str]:
        if not self.path.exists():
            return False, "No event log found."
        prev = GENESIS
        try:
            for n, event in enumerate(self.events(), 1):
                claimed = event.pop("hash", None)
                if claimed is None or "prev_hash" not in event:
                    return False, f"Event {n}: missing hash fields."
                if event["prev_hash"] != prev:
                    return False, f"Event {n}: chain broken (prev_hash mismatch)."
                if digest(event) != claimed:
                    return False, f"Event {n}: content does not match its hash."
                prev = claimed
        except EvidenceError as exc:
            return False, str(exc)
        return True, f"Chain intact; head {prev}."


class RunStore:
    """Folder holding every artefact of one intake run."""

    def __init__(self, root: Path, run_id: str | None = None):
        self.run_id = run_id or new_run_id()
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(self.dir / "events.jsonl")

    @classmethod
    def open(cls, run_dir: Path) -> RunStore:
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run folder not found: {run_dir}")
        return cls(run_dir.parent, run_dir.name)

    def path(self, name: str) -> Path:
        return self.dir / name

    def write_text(self, name: str, text: str) -> Path:
        p = self.path(name)
        p.write_text(text, encoding="utf-8")
        return p

    def write_json(self, name: str, obj: Any) -> Path:
        return self.write_text(name, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")

    def read_json(self, name: str) -> Any:
        return json.loads(self.path(name).read_text(encoding="utf-8"))
