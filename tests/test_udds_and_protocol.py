from __future__ import annotations

import numpy as np
import pytest
from conftest import fake_standard_charge_sequence

from pybamm_w10.config import RunConfig
from pybamm_w10.protocol import ProtocolStateMachine
from pybamm_w10.types import (
    FailureReason,
    PhysicalProtocolFailure,
    ProtocolPhase,
    StageOutcome,
    StageSpec,
    TerminationKind,
)
from pybamm_w10.udds import (
    build_profile_from_mat,
    CurrentProfile,
    identify_repeat_period,
    repeat_to_net_discharge,
    validate_target,
)


class FakeAgingBackend:
    run_standard_charge_sequence = fake_standard_charge_sequence

    def __init__(self, fail_step5: bool = False) -> None:
        self.q, self.time, self.fail_step5 = 10.0, 0.0, fail_step5

    def current_time_s(self): return self.time
    def discharge_capacity_ah(self): return self.q
    def _outcome(self, spec, kind=None):
        return StageOutcome(kind or spec.expected_termination, (kind or spec.expected_termination).value, self.time, None, 3.5, 296.15, self.q, "fake")
    def cc_charge_to_voltage(self, *_, spec): self.time += 100; return self._outcome(spec)
    def cv_hold_to_current(self, *_, spec): self.time += 100; return self._outcome(spec)
    def rest(self, seconds, *, spec): self.time += seconds; return self._outcome(spec)
    def discharge_to_capacity(self, _current, start, target, _voltage, *, spec):
        self.time += 100
        if self.fail_step5: return self._outcome(spec, TerminationKind.VOLTAGE)
        self.q = start + target; return self._outcome(spec)
    def drive_cycle_to_capacity(self, _profile, start, target, _voltage, *, spec): self.q = start + target; self.time += 200; return self._outcome(spec)
    def summary_metrics(self, *_): return {"terminal_voltage_v": 2.5}


def test_repeat_to_net_discharge_precisely_truncates_constant_waveform() -> None:
    base = CurrentProfile(np.array([0.0, 10.0]), np.array([3.6, 3.6]))
    profile = repeat_to_net_discharge(base, 0.035)
    validate_target(profile, 0.035)
    assert profile.net_discharge_ah == pytest.approx(0.035, abs=1e-12)
    assert np.all(np.diff(profile.time_s) > 0)


def test_repeat_truncation_solves_linear_current_segment_analytically() -> None:
    base = CurrentProfile(np.array([0.0, 10.0]), np.array([0.0, 10.0]))
    profile = repeat_to_net_discharge(base, 0.001)
    validate_target(profile, 0.001, tolerance=1e-10)
    assert profile.time_s[-1] == pytest.approx(np.sqrt(7.2), rel=1e-12)


def test_period_identification_requires_common_smallest_complete_unit() -> None:
    unit = np.sin(np.arange(0.0, 21.0) * 0.7) + np.arange(21) * 0.001
    values = np.tile(unit[:-1], 4)
    segment = CurrentProfile(np.arange(len(values), dtype=float), values)
    period, evidence = identify_repeat_period([segment, segment], 18, 22)
    assert period == 20
    assert evidence["segment_best_periods_s"] == [20, 20]


def test_step5_and_step6_share_one_capacity_window_baseline() -> None:
    base = CurrentProfile(np.array([0.0, 3600.0]), np.array([1.0, 1.0]))
    backend = FakeAgingBackend()
    result = ProtocolStateMachine(RunConfig(), base).run_standard_cycle(backend, 1, 4.0, 0)
    assert result.step5_target_ah == pytest.approx(0.8)
    assert result.delta_q5_actual_ah == pytest.approx(0.8)
    assert result.actual_udds_remaining_target_ah == pytest.approx(2.4)
    assert result.udds_actual_ah == pytest.approx(2.4)
    assert result.udds_profile_available_ah > result.actual_udds_remaining_target_ah
    assert result.udds_guard_ah > 0
    assert result.window_actual_ah == pytest.approx(3.2)


def test_voltage_before_step5_capacity_is_physical_protocol_failure() -> None:
    base = CurrentProfile(np.array([0.0, 3600.0]), np.array([1.0, 1.0]))
    with pytest.raises(PhysicalProtocolFailure) as exc:
        ProtocolStateMachine(RunConfig(), base).run_standard_cycle(FakeAgingBackend(True), 1, 4.0, 0)
    assert exc.value.context.reason is FailureReason.PHYSICAL_EVENT_BEFORE_TARGET
    assert exc.value.context.phase is ProtocolPhase.STEP5_C4_DISCHARGE


def test_strict_special_cycle_includes_precompleted_recovery_stages() -> None:
    base = CurrentProfile(np.array([0.0, 3600.0]), np.array([1.0, 1.0]))
    backend = FakeAgingBackend()
    backend.time = 500.0
    result = ProtocolStateMachine(RunConfig(mode="strict-w10"), base).run_standard_cycle(
        backend,
        1,
        4.0,
        0,
        charge_already_complete=True,
        cycle_start_time_s=0.0,
        precompleted_stage_durations={"rpt_recovery_cc": 100.0, "rpt_recovery_cv": 200.0, "rpt_recovery_rest": 200.0},
    )
    assert result.start_time_s == 0.0
    assert result.stage_durations_s["rpt_recovery_rest"] == 200.0
    assert "3c_cc" not in result.stage_durations_s


def test_real_w10_all_segments_identify_2600s_and_175_units() -> None:
    path = RunConfig(data_root=__import__("pathlib").Path(r"E:\battery\data")).normalized(__import__("pathlib").Path.cwd()).w10_mat_path
    profile, evidence = build_profile_from_mat(path)
    period = evidence["period_identification"]
    assert period["selected_period_s"] == 2600
    assert period["segment_best_periods_s"] == [2600] * 25
    assert evidence["complete_unit_count"] == 175
    assert profile.time_s[-1] == 2600
