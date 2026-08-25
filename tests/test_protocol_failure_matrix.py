from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from pybamm_w10.config import RunConfig
from pybamm_w10.protocol import ProtocolStateMachine
from pybamm_w10.types import (
    FailureReason,
    NumericalFailure,
    PhysicalProtocolFailure,
    StageOutcome,
    StageSpec,
    TerminationKind,
)
from pybamm_w10.udds import CurrentProfile
from conftest import fake_standard_charge_sequence


class MatrixBackend:
    run_standard_charge_sequence = fake_standard_charge_sequence

    def __init__(self, step6_kind: TerminationKind = TerminationKind.CAPACITY, *, capacity_error: bool = False, invalid_state: bool = False, solver_failure: bool = False) -> None:
        self.q = 10.0
        self.time = 0.0
        self.step6_kind = step6_kind
        self.capacity_error = capacity_error
        self.invalid_state = invalid_state
        self.solver_failure = solver_failure

    def current_time_s(self) -> float:
        return self.time

    def discharge_capacity_ah(self) -> float:
        return self.q

    def _outcome(self, spec: StageSpec, kind: TerminationKind | None = None) -> StageOutcome:
        selected = kind or spec.expected_termination
        self.time += 10.0
        voltage = float("nan") if self.invalid_state else 3.5
        return StageOutcome(
            selected,
            selected.value,
            self.time,
            None,
            voltage,
            296.15,
            self.q,
            "state",
        )

    def cc_charge_to_voltage(self, *_args, spec: StageSpec) -> StageOutcome:
        return self._outcome(spec)

    def cv_hold_to_current(self, *_args, spec: StageSpec) -> StageOutcome:
        return self._outcome(spec)

    def rest(self, *_args, spec: StageSpec) -> StageOutcome:
        return self._outcome(spec)

    def discharge_to_capacity(self, _current, start, target, _voltage, *, spec: StageSpec) -> StageOutcome:
        self.q = start + target * (1.02 if self.capacity_error else 1.0)
        return self._outcome(spec)

    def drive_cycle_to_capacity(self, _profile, start, target, _voltage, *, spec: StageSpec) -> StageOutcome:
        if self.solver_failure:
            raise RuntimeError("injected solver failure")
        if self.step6_kind is TerminationKind.CAPACITY:
            self.q = start + target * (1.02 if self.capacity_error else 1.0)
        return self._outcome(spec, self.step6_kind)

    def summary_metrics(self, *_args):
        return {"terminal_voltage_v": 3.5}


def _machine() -> ProtocolStateMachine:
    base = CurrentProfile(np.array([0.0, 3600.0]), np.array([1.0, 1.0]))
    return ProtocolStateMachine(RunConfig(), base)


def _run(backend: MatrixBackend):
    return _machine().run_standard_cycle(backend, cycle=1, q_ref_ah=4.0, q_ref_node=0)


def test_capacity_termination_is_the_only_step6_success() -> None:
    result = _run(MatrixBackend())
    assert result.termination_classification == "EXPECTED_PROTOCOL_EVENT"
    assert result.udds_actual_ah == pytest.approx(2.4)
    assert result.stage_durations_s["step6_udds"] == pytest.approx(10.0)
    assert result.stage_wall_clock_durations_s["step6_udds"] >= 0.0


def test_standard_cycle_reports_each_named_stage_in_order() -> None:
    observed: list[tuple[str, str]] = []
    machine = _machine()
    machine.on_stage_change = lambda phase, stage: observed.append((phase.value, stage))

    machine.run_standard_cycle(MatrixBackend(), cycle=1, q_ref_ah=4.0, q_ref_node=0)

    assert [stage for _, stage in observed] == [
        "3c_cc",
        "4v_cv",
        "c4_cc",
        "4p2v_cv",
        "post_charge_rest",
        "step5_c4_discharge",
        "step6_udds",
    ]


@pytest.mark.parametrize("kind", [TerminationKind.VOLTAGE, TerminationKind.MODEL_PHYSICAL_EVENT])
def test_physical_boundaries_before_target_are_physical_failures(kind: TerminationKind) -> None:
    with pytest.raises(PhysicalProtocolFailure) as exc:
        _run(MatrixBackend(kind))
    assert exc.value.context.reason is FailureReason.PHYSICAL_EVENT_BEFORE_TARGET


def test_final_time_is_a_numerical_failure() -> None:
    with pytest.raises(NumericalFailure) as exc:
        _run(MatrixBackend(TerminationKind.FINAL_TIME))
    assert exc.value.context.reason is FailureReason.UNEXPECTED_FINAL_TIME


def test_unknown_termination_is_a_numerical_failure() -> None:
    with pytest.raises(NumericalFailure) as exc:
        _run(MatrixBackend(TerminationKind.UNKNOWN))
    assert exc.value.context.reason is FailureReason.UNKNOWN_TERMINATION


def test_capacity_error_is_a_numerical_failure() -> None:
    with pytest.raises(NumericalFailure) as exc:
        _run(MatrixBackend(capacity_error=True))
    assert exc.value.context.reason is FailureReason.CAPACITY_TOLERANCE_FAILURE


def test_non_finite_terminal_state_is_a_numerical_failure() -> None:
    with pytest.raises(NumericalFailure) as exc:
        _run(MatrixBackend(invalid_state=True))
    assert exc.value.context.reason is FailureReason.INVALID_STATE


def test_solver_exception_is_a_numerical_failure() -> None:
    with pytest.raises(NumericalFailure) as exc:
        _run(MatrixBackend(solver_failure=True))
    assert exc.value.context.reason is FailureReason.SOLVER_FAILURE
