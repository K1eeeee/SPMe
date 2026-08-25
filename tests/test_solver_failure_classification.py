from __future__ import annotations

import pytest

from pybamm_w10.types import SolverStepFailure, classify_sundials_error


@pytest.mark.parametrize("code", ("IDA_ERR_FAIL", "IDA_CONV_FAIL", "IDA_LSETUP_FAIL"))
def test_retryable_sundials_errors_are_classified_once(code: str) -> None:
    assert classify_sundials_error(f"prefix {code}: details") == (code, True)


def test_unknown_solver_error_is_not_retryable() -> None:
    assert classify_sundials_error("IDA_TOO_MUCH_WORK") == ("IDA_TOO_MUCH_WORK", False)
    failure = SolverStepFailure.from_exception(RuntimeError("IDA_TOO_MUCH_WORK"), 1, "4v_cv")
    assert failure.retryable is False
    assert failure.charge_stage == "4v_cv"
