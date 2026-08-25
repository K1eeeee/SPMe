from __future__ import annotations

import json

import pytest

from pybamm_w10.calibration.workflow import (
    CalibrationState,
    CalibrationTransitionError,
    CalibrationWorkflow,
)


def test_workflow_writes_atomic_status_and_honestly_blocks_aging(workspace_tmp) -> None:
    workflow = CalibrationWorkflow(workspace_tmp / "calibration", parameter_fingerprint="a" * 64)
    workflow.transition(CalibrationState.CAPACITY_CALIBRATION_READY)
    workflow.record_capacity_result({"relative_error": 0.001})
    workflow.apply_aging_data_gate({"status": "AGING_DATA_INCOMPLETE", "reason": "MISSING_W10_HPPC_EIS"})

    status = json.loads((workspace_tmp / "calibration" / "calibration_status.json").read_text(encoding="utf-8"))
    assert status["state"] == "AGING_DATA_INCOMPLETE"
    assert status["reason"] == "MISSING_W10_HPPC_EIS"
    assert (workspace_tmp / "calibration" / "capacity_calibration.json").is_file()
    with pytest.raises(CalibrationTransitionError):
        workflow.transition(CalibrationState.PARAMETERS_FROZEN)
