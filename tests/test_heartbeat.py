from __future__ import annotations

import json
import time

from pybamm_w10.progress import Heartbeat, ProgressState
from pybamm_w10.output import build_output_manifest


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_heartbeat_writes_periodic_phase_and_terminal_state(workspace_tmp) -> None:
    path = workspace_tmp / "run_progress.json"
    heartbeat = Heartbeat(path, interval_s=0.01)
    heartbeat.start(ProgressState(phase="PREFLIGHT", completed_cycles=0))
    initial = _read(path)
    assert initial["status"] == "RUNNING"
    assert initial["phase"] == "PREFLIGHT"

    heartbeat.update(
        ProgressState(
            phase="STEP6_UDDS",
            stage="step6_udds",
            completed_cycles=3,
        )
    )
    changed = _read(path)
    assert changed["phase"] == "STEP6_UDDS"
    assert changed["stage"] == "step6_udds"
    assert changed["completed_cycles"] == 3

    time.sleep(0.03)
    periodic = _read(path)
    assert periodic["heartbeat_sequence"] > changed["heartbeat_sequence"]

    heartbeat.terminate("COMPLETED")
    terminal = _read(path)
    assert terminal["status"] == "TERMINATED"
    assert terminal["business_status"] == "COMPLETED"
    assert not heartbeat.is_running


def test_heartbeat_context_manager_terminates_after_exception(workspace_tmp) -> None:
    path = workspace_tmp / "run_progress.json"
    heartbeat = Heartbeat(path, interval_s=1)
    try:
        with heartbeat.lifecycle(ProgressState(phase="PREFLIGHT")):
            raise RuntimeError("test failure")
    except RuntimeError:
        pass
    assert _read(path)["status"] == "TERMINATED"


def test_progress_file_is_not_part_of_checkpoint_manifest(workspace_tmp) -> None:
    (workspace_tmp / "run_progress.json").write_text("{}", encoding="utf-8")
    manifest = build_output_manifest(workspace_tmp, 1, 0, None)
    assert "run_progress.json" not in manifest.artifacts


def test_progress_includes_solver_attempt_without_changing_business_phase(workspace_tmp) -> None:
    path = workspace_tmp / "run_progress.json"
    heartbeat = Heartbeat(path, interval_s=1)
    heartbeat.start(ProgressState(
        phase="STANDARD_CHARGE", stage="4v_cv", current_cycle=10,
        completed_cycles=9, transaction=10, solver_attempt=2,
        solver_profile="conservative_cv_transition",
    ))
    heartbeat.terminate("NUMERICAL_FAILURE")
    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert payload["stage"] == "4v_cv"
    assert payload["solver_attempt"] == 2
    assert payload["solver_profile"] == "conservative_cv_transition"
    assert payload["status"] == "TERMINATED"
