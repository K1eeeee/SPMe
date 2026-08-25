from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from pybamm_w10.config import RunConfig
from pybamm_w10.types import TerminationKind
from pybamm_w10.udds import CurrentProfile


@dataclass(frozen=True)
class _Snapshot:
    state_hash: str


@dataclass
class _Initial:
    fingerprint: str = "initial-fingerprint"


class _Backend:
    def __init__(self, *_args) -> None:
        self.time = 0.0
        self.capacity = 0.0
        self.last_termination = "event: W10_CAPACITY_WINDOW"
        self.solution = SimpleNamespace(y=np.array([[0.0]]))

    def snapshot(self):
        return _Snapshot(f"state-{self.time:.6f}-{self.capacity:.6f}")

    def restore(self, snapshot) -> None:
        _, time_value, capacity_value = snapshot.state_hash.split("-")
        self.time, self.capacity = float(time_value), float(capacity_value)
        self.solution = SimpleNamespace(y=np.array([[self.capacity]]))

    def fork(self):
        copied = _Backend()
        copied.time, copied.capacity = self.time, self.capacity
        copied.solution = SimpleNamespace(y=self.solution.y.copy())
        return copied

    def current_time_s(self) -> float:
        return self.time

    def discharge_capacity_ah(self) -> float:
        return self.capacity

    def discharge_to_capacity(self, _current, _start, target, _voltage, **_kwargs):
        self.capacity += target
        self.time += target * 3600
        self.solution = SimpleNamespace(y=np.array([[self.capacity]]))
        return SimpleNamespace(
            termination_kind=TerminationKind.CAPACITY,
            raw_termination=self.last_termination,
            termination_time_s=self.time,
            termination_value=target,
            state_hash=self.snapshot().state_hash,
        )

    def drive_cycle_to_capacity(self, _profile, _start, target, _voltage, **_kwargs):
        return self.discharge_to_capacity(0, 0, target, 0)

    def timeseries(self):
        return {"time_s": [0.0, self.time], "current_a": [0.0, 0.0]}


def test_smoke_uses_production_window_and_never_writes_aging_results(workspace_tmp, monkeypatch) -> None:
    import pybamm_w10.smoke as smoke

    profile = CurrentProfile(np.array([0.0, 1.0]), np.array([1.0, 1.0]))
    monkeypatch.setattr(smoke.W10Runner, "prepare_profile", lambda _self: (profile, {"ok": True}))
    monkeypatch.setattr(smoke, "build_spme", lambda _config: SimpleNamespace())
    monkeypatch.setattr(smoke, "environment_metadata", lambda _artifacts: {"pybamm": "fake"})
    monkeypatch.setattr(smoke, "construct_initial_state_record", lambda *_args: _Initial())
    monkeypatch.setattr(smoke, "PyBaMMBackend", _Backend)
    monkeypatch.setattr(smoke, "effective_parameters_audit", lambda *_args: {"fingerprint": "audit"})
    monkeypatch.setattr(smoke, "effective_parameters_fingerprint", lambda _audit: "audit")
    monkeypatch.setattr(smoke, "_hash_file", lambda _path: "input")
    monkeypatch.setattr(smoke, "_verify_lock_contention", lambda *_args: None)

    run_dir = smoke.run_smoke(RunConfig(), workspace_tmp / "smoke")
    report = __import__("json").loads((run_dir / "smoke_report.json").read_text(encoding="utf-8"))

    assert report["status"] == "PASSED"
    assert report["no_aging_cycles_executed"] is True
    assert report["udds_capacity_event"]["event_time_s"] < report["udds_capacity_event"]["profile_end_s"]
    assert report["udds_capacity_event"]["guard_ah"] > 0
    assert not (run_dir / "cycle_summary.csv").exists()
    assert sorted(path.name for path in (run_dir / "checkpoints").glob("cycle-*.pkl")) == ["cycle-000.pkl"]
    assert __import__("json").loads((run_dir / "run_progress.json").read_text())["status"] == "TERMINATED"
