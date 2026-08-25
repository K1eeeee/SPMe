from __future__ import annotations

from types import SimpleNamespace

import pytest

from pybamm_w10.backend import PyBaMMBackend, PyBaMMSnapshot
from pybamm_w10.config import RunConfig
from pybamm_w10.types import (
    SolverStepFailure,
    StageOutcome,
    StandardChargeSequenceResult,
    TerminationKind,
)


def _result(profile: str) -> StandardChargeSequenceResult:
    outcomes = tuple(
        StageOutcome(kind, "event", float(index + 1), 1.0, 4.0, 298.0, 0.0, "state")
        for index, kind in enumerate(
            (TerminationKind.VOLTAGE, TerminationKind.CURRENT,
             TerminationKind.VOLTAGE, TerminationKind.CURRENT)
        )
    )
    return StandardChargeSequenceResult(
        outcomes=outcomes,
        traces=(),
        stage_durations_s={},
        stage_wall_clock_durations_s={},
        terminal_snapshot=PyBaMMSnapshot(None, 0.0, 0.0, 0.0, "unbuilt-initial"),
        attempt_count=1,
        solver_profile=profile,
    )


def test_retryable_failure_restarts_once_with_conservative_profile(monkeypatch) -> None:
    backend = PyBaMMBackend(SimpleNamespace())
    profiles = []

    def solve(*_args, **kwargs):
        profile = _args[2]
        profiles.append(profile.name)
        if len(profiles) == 1:
            raise SolverStepFailure.from_exception(
                RuntimeError("IDA_ERR_FAIL: injected"), 1, "4v_cv"
            )
        return _result(profile.name)

    monkeypatch.setattr(backend, "_solve_standard_charge_attempt", solve)
    result = backend.run_standard_charge_sequence(RunConfig())

    assert profiles == ["certified_charge", "certified_charge_retry"]
    assert result.attempt_count == 2
    assert result.initial_failure_code == "IDA_ERR_FAIL"
    assert len(result.attempt_failures) == 1


def test_non_retryable_failure_is_not_retried(monkeypatch) -> None:
    backend = PyBaMMBackend(SimpleNamespace())
    calls = 0

    def solve(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise SolverStepFailure.from_exception(
            RuntimeError("IDA_TOO_MUCH_WORK"), 1, "4v_cv"
        )

    monkeypatch.setattr(backend, "_solve_standard_charge_attempt", solve)
    with pytest.raises(SolverStepFailure) as caught:
        backend.run_standard_charge_sequence(RunConfig())

    assert calls == 1
    assert len(caught.value.attempt_failures) == 1
