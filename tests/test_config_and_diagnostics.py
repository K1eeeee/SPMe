from __future__ import annotations

import copy
import pytest

from pybamm_w10.config import RunConfig
from pybamm_w10.diagnostics import capacity_targets, run_capacity_rpt, run_eis, run_hppc
from pybamm_w10.types import (
    FailureReason,
    NumericalFailure,
    PhysicalProtocolFailure,
    StageOutcome,
    StageSpec,
    TerminationKind,
)


class FakeDiagnosticBackend:
    def __init__(self) -> None:
        self.q = 7.0
        self.time = 0.0
        self.state = 0

    def snapshot(self):
        return self.state, self.time, self.q

    def fork(self):
        return copy.deepcopy(self)

    def restore(self, value) -> None:
        self.state, self.time, self.q = value

    def current_time_s(self) -> float:
        return self.time

    def _outcome(self, spec: StageSpec, kind: TerminationKind | None = None) -> StageOutcome:
        self.state += 1; self.time += 60
        return StageOutcome(
            termination_kind=kind or spec.expected_termination,
            raw_termination=(kind or spec.expected_termination).value,
            termination_time_s=self.time,
            termination_value=None,
            terminal_voltage_v=3.5,
            terminal_temperature_k=296.15,
            terminal_discharge_capacity_ah=self.q,
            state_hash=f"state-{self.state}",
        )

    def cc_charge_to_voltage(self, *_, spec: StageSpec) -> StageOutcome:
        return self._outcome(spec)

    def cv_hold_to_current(self, *_, spec: StageSpec) -> StageOutcome:
        return self._outcome(spec)

    def rest(self, seconds, *, spec: StageSpec) -> StageOutcome:
        self.time += seconds - 60
        return self._outcome(spec)

    def discharge_capacity_ah(self) -> float:
        return self.q

    def discharge_to_voltage(self, *_args, spec: StageSpec) -> StageOutcome:
        self.q += 4.2
        return self._outcome(spec)

    def timeseries_since(self, _start):
        return {"time_s": [0.0, self.time], "current_a": [0.0, 0.0]}


def test_cylindrical_geometry_and_targets() -> None:
    config = RunConfig()
    assert config.cell.volume_m3 == pytest.approx(2.556e-5, rel=2e-3)
    assert config.cell.cooling_surface_area_m2 == pytest.approx(0.005491, rel=2e-3)
    target = capacity_targets(4.85, 0.97, config)
    assert target.step5_target_ah == pytest.approx(0.97)
    assert target.window_target_ah == pytest.approx(3.88)
    assert target.udds_remaining_ah == pytest.approx(2.91)


def test_virtual_rpt_discards_branch_but_retains_qref() -> None:
    backend = FakeDiagnosticBackend()
    before = backend.snapshot()
    result = run_capacity_rpt(backend, 0, RunConfig(), None, virtual=True)
    assert backend.snapshot() == before
    assert result.capacity_ah == pytest.approx(4.2)
    assert result.changed_main_state is False
    assert result.main_state_hash_before == result.main_state_hash_after
    assert result.main_time_before_s == result.main_time_after_s == 0.0
    assert result.diagnostic_duration_s > 0
    assert result.became_q_ref is True
    assert result.targets and result.targets.window_target_ah == pytest.approx(3.36)


def test_strict_rpt_changes_state_and_final_node_does_not_make_control_targets() -> None:
    backend = FakeDiagnosticBackend()
    result = run_capacity_rpt(backend, 350, RunConfig(mode="strict-w10"), 4.2, virtual=False)
    assert result.changed_main_state is True
    assert result.targets is None
    assert result.became_q_ref is False


class FailingRPTBackend(FakeDiagnosticBackend):
    def __init__(self, failure_kind: TerminationKind) -> None:
        super().__init__()
        self.failure_kind = failure_kind

    def discharge_to_voltage(self, *_args, spec: StageSpec) -> StageOutcome:
        self.q += 1.0
        return self._outcome(spec, self.failure_kind)


def test_virtual_rpt_physical_boundary_rejects_qref_without_touching_main_state() -> None:
    backend = FailingRPTBackend(TerminationKind.MODEL_PHYSICAL_EVENT)
    before = backend.snapshot()

    with pytest.raises(PhysicalProtocolFailure) as exc:
        run_capacity_rpt(backend, 25, RunConfig(), 4.2, virtual=True)

    assert exc.value.context.reason is FailureReason.PHYSICAL_EVENT_BEFORE_TARGET
    assert exc.value.context.phase.value == "RPT_CAPACITY_DISCHARGE"
    assert backend.snapshot() == before


def test_rpt_unknown_termination_is_a_numerical_failure() -> None:
    backend = FailingRPTBackend(TerminationKind.UNKNOWN)

    with pytest.raises(NumericalFailure) as exc:
        run_capacity_rpt(backend, 25, RunConfig(mode="strict-w10"), 4.2, virtual=False)

    assert exc.value.context.reason is FailureReason.UNKNOWN_TERMINATION


def test_unimplemented_diagnostic_interfaces_fail_explicitly() -> None:
    with pytest.raises(NotImplementedError):
        run_hppc()
    with pytest.raises(NotImplementedError):
        run_eis()
