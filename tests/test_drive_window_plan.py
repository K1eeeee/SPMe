from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from pybamm_w10.config import RunConfig
from pybamm_w10.udds import CurrentProfile, build_drive_window_plan


def _constant_profile(current_a: float) -> CurrentProfile:
    return CurrentProfile(np.array([0.0, 3600.0]), np.array([current_a, current_a]))


def test_relative_guard_dominates_and_event_is_inside_profile() -> None:
    plan = build_drive_window_plan(
        _constant_profile(1.0), remaining_ah=1.0, max_step_s=0.01, config=RunConfig()
    )

    assert plan.guard_ah == pytest.approx(0.005)
    assert plan.profile_available_ah == pytest.approx(1.005)
    assert plan.event_target_ah == pytest.approx(1.0)
    assert plan.profile.net_discharge_ah > plan.event_target_ah
    assert plan.event_time_s < plan.profile.time_s[-1]


def test_solver_step_guard_dominates() -> None:
    plan = build_drive_window_plan(
        _constant_profile(36.0), remaining_ah=0.1, max_step_s=1.0, config=RunConfig()
    )

    assert plan.guard_ah == pytest.approx(0.1)
    assert plan.profile_available_ah == pytest.approx(0.2)


def test_plan_is_deterministic_and_rejects_invalid_targets() -> None:
    base = _constant_profile(2.0)
    config = replace(RunConfig(), udds_event_guard_fraction=0.01)
    first = build_drive_window_plan(base, remaining_ah=0.25, max_step_s=0.5, config=config)
    second = build_drive_window_plan(base, remaining_ah=0.25, max_step_s=0.5, config=config)

    assert first.profile.fingerprint == second.profile.fingerprint
    assert first.profile_fingerprint == second.profile_fingerprint
    for invalid in (0.0, -0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            build_drive_window_plan(base, remaining_ah=invalid, max_step_s=0.5, config=config)
    for invalid in (0.0, -0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            build_drive_window_plan(base, remaining_ah=0.25, max_step_s=invalid, config=config)


def test_cycle_zero_style_target_is_not_coincident_with_curve_end() -> None:
    plan = build_drive_window_plan(
        _constant_profile(1.0), remaining_ah=2.4, max_step_s=1.0, config=RunConfig()
    )

    assert plan.event_time_s < plan.profile.time_s[-1]
    assert plan.profile_available_ah > plan.remaining_ah
