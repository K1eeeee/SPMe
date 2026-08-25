from __future__ import annotations

import pickle

import pytest

from pybamm_w10.config import RunConfig
from pybamm_w10.output import RunDirectoryLock, load_checkpoint, write_failure_artifacts
from pybamm_w10.types import FailureContext, FailureReason, NumericalFailure, RunStatus


def test_failure_artifacts_are_atomic_forensic_only_and_not_resumable(workspace_tmp) -> None:
    context = FailureContext(reason=FailureReason.SOLVER_FAILURE, cycle=7, message="boom")
    json_path, pickle_path = write_failure_artifacts(workspace_tmp, context)
    payload = __import__("json").loads(json_path.read_text(encoding="utf-8"))
    assert payload["reason"] == "SOLVER_FAILURE"
    with pickle_path.open("rb") as handle:
        forensic = pickle.load(handle)
    assert forensic["forensic_only"] is True
    assert forensic["resume_eligible"] is False
    with pytest.raises(TypeError, match="failure snapshot"):
        load_checkpoint(pickle_path, RunConfig(), "udds")


def test_lock_records_business_terminal_status_before_handle_release(workspace_tmp) -> None:
    with RunDirectoryLock(workspace_tmp, {"mode": "test"}) as lock:
        lock.set_business_status(RunStatus.NUMERICAL_FAILURE)
    metadata = __import__("json").loads((workspace_tmp / ".run.lock").read_text(encoding="utf-8"))
    assert metadata["business_status"] == "NUMERICAL_FAILURE"
    assert metadata["release_reason"] == "normal"
