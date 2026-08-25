from __future__ import annotations

import pytest
from conftest import fake_standard_charge_sequence

from pybamm_w10.config import RunConfig
from pybamm_w10.runner import W10Runner
from pybamm_w10.types import (
    ChargeStageTrace,
    FailureReason,
    PhysicalProtocolFailure,
    StageOutcome,
    StageSpec,
    TerminationKind,
)


class RecoveryBackend:
    run_standard_charge_sequence = fake_standard_charge_sequence

    def __init__(self, cc_kind: TerminationKind = TerminationKind.VOLTAGE) -> None:
        self.time = 0.0
        self.cc_kind = cc_kind
        self.calls: list[str] = []

    def current_time_s(self) -> float:
        return self.time

    def _outcome(self, spec: StageSpec, kind: TerminationKind | None = None) -> StageOutcome:
        self.time += 10.0
        selected = kind or spec.expected_termination
        return StageOutcome(
            termination_kind=selected,
            raw_termination=selected.value,
            termination_time_s=self.time,
            termination_value=None,
            terminal_voltage_v=3.5,
            terminal_temperature_k=296.15,
            terminal_discharge_capacity_ah=4.0,
            state_hash=f"state-{len(self.calls)}",
        )

    def cc_charge_to_voltage(self, *_, spec: StageSpec) -> StageOutcome:
        self.calls.append("cc")
        return self._outcome(spec, self.cc_kind)

    def cv_hold_to_current(self, *_, spec: StageSpec) -> StageOutcome:
        self.calls.append("cv")
        return self._outcome(spec)

    def rest(self, *_, spec: StageSpec) -> StageOutcome:
        self.calls.append("rest")
        return self._outcome(spec)

    def extract_charge_stage_trace(self, name, start, end, _resolved):
        potential = (0.20, 0.10) if name == "rpt_recovery_cc" else (0.09, 0.08)
        return ChargeStageTrace(
            name,
            (start, end),
            {"negative_electrode_surface_potential_difference_v": potential},
        )


def test_strict_rpt_recovery_stops_on_a_physical_boundary(workspace_tmp) -> None:
    runner = W10Runner(RunConfig(mode="strict-w10"), workspace_tmp)
    backend = RecoveryBackend(TerminationKind.MODEL_PHYSICAL_EVENT)

    with pytest.raises(PhysicalProtocolFailure) as exc:
        runner._post_rpt_recovery(backend, rpt_node=25)

    assert exc.value.context.reason is FailureReason.PHYSICAL_EVENT_BEFORE_TARGET
    assert exc.value.context.rpt_node == 25
    assert backend.calls == ["cc"]


def test_strict_rpt_recovery_records_all_completed_stage_durations(workspace_tmp) -> None:
    runner = W10Runner(RunConfig(mode="strict-w10"), workspace_tmp)
    started, durations, metrics = runner._post_rpt_recovery(RecoveryBackend(), rpt_node=25)

    assert started == 0.0
    assert durations == {
        "rpt_recovery_cc": 10.0,
        "rpt_recovery_cv": 10.0,
        "rpt_recovery_rest": 10.0,
    }
    assert metrics == {}


def test_strict_rpt_recovery_retains_charge_only_minimum_potential(workspace_tmp) -> None:
    runner = W10Runner(RunConfig(mode="strict-w10"), workspace_tmp)

    _, _, metrics = runner._post_rpt_recovery(
        RecoveryBackend(), rpt_node=25, resolved_charge_variables=object()
    )

    assert metrics["negative_electrode_min_potential_v"] == 0.08
