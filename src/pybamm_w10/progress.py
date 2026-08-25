"""Thread-safe, solution-free heartbeat records for long W10 runs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Iterator

from .output import write_json


@dataclass(frozen=True)
class ProgressState:
    """Small immutable state that is safe to retain outside the solver."""

    phase: str
    stage: str | None = None
    completed_cycles: int = 0
    transaction: int = 0
    current_cycle: int | None = None
    solver_attempt: int = 1
    solver_profile: str = "general_protocol"


class Heartbeat:
    """Atomically persist liveness without retaining a PyBaMM solution."""

    def __init__(self, path: Path, *, interval_s: float) -> None:
        if interval_s <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.path = path
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = ProgressState(phase="PREFLIGHT")
        self._business_status = "RUNNING"
        self._terminal = False
        self._sequence = 0
        self._last_write_error: OSError | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _write(self) -> None:
        with self._lock:
            self._sequence += 1
            payload = {
                **asdict(self._state),
                "status": "TERMINATED" if self._terminal else "RUNNING",
                "business_status": self._business_status,
                "heartbeat_sequence": self._sequence,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            for attempt in range(5):
                try:
                    write_json(self.path, payload)
                    self._last_write_error = None
                    return
                except PermissionError as exc:
                    self._last_write_error = exc
                    if attempt == 4:
                        raise
                    time.sleep(0.01)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._write()
            except OSError as exc:
                # A transient external reader must not terminate the run thread.
                self._last_write_error = exc

    def start(self, state: ProgressState) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat is already running")
        with self._lock:
            self._state = state
        self._write()
        self._thread = threading.Thread(target=self._run, name="pybamm-w10-heartbeat", daemon=True)
        self._thread.start()

    def update(self, state: ProgressState) -> None:
        with self._lock:
            if self._terminal:
                raise RuntimeError("cannot update a terminated heartbeat")
            self._state = state
        self._write()

    def terminate(self, business_status: str) -> None:
        if self._terminal:
            return
        with self._lock:
            self._business_status = str(business_status)
            self._terminal = True
        self._write()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_s * 2))

    @contextmanager
    def lifecycle(self, state: ProgressState) -> Iterator["Heartbeat"]:
        self.start(state)
        try:
            yield self
        except Exception:
            self.terminate("NUMERICAL_FAILURE")
            raise
        else:
            self.terminate("COMPLETED")
