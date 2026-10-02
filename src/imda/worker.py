"""Scheduler loop for ``imda worker``: run jobs when they are due, sleep in small steps.

The clock, the sleep and the stop flag are arguments, so tests drive the loop with a fake clock.
A job that raises is logged and tried again at its next due time; it never stops the loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

STEP_SECONDS = 5.0
"""Longest single sleep. Keeps the loop quick to react to a stop request."""
MAX_ERROR_LENGTH = 200

JobOutcome = tuple[bool, Mapping[str, object]]
Record = dict[str, object]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    name: str
    every_seconds: float
    run: Callable[[], JobOutcome]
    """Returns ``(ok, detail)``. Any exception is treated as a failed run."""


def _execute(job: ScheduledJob, clock: Callable[[], float]) -> Record:
    started = clock()
    record: Record
    try:
        ok, detail = job.run()
        record = {"status": "ok" if ok else "failed", **detail}
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
        record = {"status": "error", "error": message}
    record["duration_s"] = round(clock() - started, 3)
    return record


def run_worker(
    jobs: Sequence[ScheduledJob],
    *,
    emit: Callable[[Record], None],
    once: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> bool:
    """Run until stopped (or one pass with ``once``). Returns False if any run failed in a
    ``once`` pass; a long-running worker returns True on a clean stop.

    Every job is due at start. Each cycle that ran something emits one record:
    ``{"event": "worker.cycle", "<job>": {...}}``.
    """
    due = {job.name: clock() for job in jobs}
    all_ok = True
    emit({"event": "worker.start", "jobs": {j.name: j.every_seconds for j in jobs}, "once": once})
    try:
        while not should_stop():
            now = clock()
            ran: Record = {}
            for job in jobs:
                if now >= due[job.name]:
                    result = _execute(job, clock)
                    all_ok = all_ok and result["status"] == "ok"
                    ran[job.name] = result
                    due[job.name] = clock() + job.every_seconds
            if ran:
                emit({"event": "worker.cycle", **ran})
            if once:
                return all_ok
            sleep(max(0.0, min(min(due.values()) - clock(), STEP_SECONDS)))
    except KeyboardInterrupt:
        pass
    emit({"event": "worker.stop"})
    return True
