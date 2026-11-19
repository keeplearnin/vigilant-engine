"""Background jobs for the local web UI (planning and publishing take minutes).

One job per run at a time: a run is locked while its job runs, so two writers never
touch the same run folder at once.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


class RunBusyError(RuntimeError):
    pass


@dataclass
class Job:
    kind: str
    started: float
    done: bool = False
    error: str | None = None

    @property
    def elapsed(self) -> int:
        return int(time.monotonic() - self.started)


class JobRunner:
    def __init__(self, *, inline: bool = False, workers: int = 2):
        self.inline = inline  # tests run jobs synchronously
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = None if inline else ThreadPoolExecutor(max_workers=workers)

    def get(self, run_id: str) -> Job | None:
        return self._jobs.get(run_id)

    def running(self, run_id: str) -> bool:
        job = self._jobs.get(run_id)
        return job is not None and not job.done

    def submit(self, run_id: str, kind: str, fn: Callable[[], None]) -> Job:
        with self._lock:
            if self.running(run_id):
                raise RunBusyError(f"This run is busy ({self._jobs[run_id].kind}).")
            job = Job(kind=kind, started=time.monotonic())
            self._jobs[run_id] = job

        def wrapped() -> None:
            try:
                fn()
            except Exception as exc:  # surfaced on the run page, never swallowed silently
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.done = True

        if self._pool is None:
            wrapped()
        else:
            self._pool.submit(wrapped)
        return job

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
