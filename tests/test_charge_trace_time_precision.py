from __future__ import annotations

import numpy as np
import pytest

from pybamm_w10.charge_efficiency import build_charge_analysis_bundle
from pybamm_w10.types import ChargeStageTrace


def _trace(
    name: str,
    time_s: tuple[float, ...],
    *,
    global_time_offset_s: float,
) -> ChargeStageTrace:
    faraday = 96485.33212
    q_ref_ah = 4.0
    lithium = tuple(
        0.8 * 3600.0 * q_ref_ah * time / (4.0 * faraday)
        for time in time_s
    )
    count = len(time_s)
    return ChargeStageTrace(
        name,
        time_s,
        {
            "current_a": (-1.0,) * count,
            "terminal_voltage_v": tuple(3.5 + 0.1 * time for time in time_s),
            "temperature_k": tuple(296.15 + 0.01 * time for time in time_s),
            "negative_particle_lithium_mol": lithium,
            "total_plating_inventory_ah": (0.0,) * count,
            "dead_lithium_inventory_ah": (0.0,) * count,
            "reversible_plating_inventory_ah": (0.0,) * count,
            "cumulative_sei_loss_ah": (0.0,) * count,
            "negative_electrode_surface_potential_difference_v": (0.1,) * count,
        },
        global_time_offset_s=global_time_offset_s,
    )


def test_charge_analysis_preserves_local_points_when_global_float_times_collide() -> None:
    global_origin_s = 300_000.0
    near_three_s = float(np.nextafter(3.0, np.inf))
    assert near_three_s > 3.0
    assert global_origin_s + near_three_s == global_origin_s + 3.0

    traces = (
        _trace("3c_cc", (0.0, 1.0), global_time_offset_s=global_origin_s),
        _trace("4v_cv", (1.0, 2.0), global_time_offset_s=global_origin_s),
        _trace("c4_cc", (2.0, 3.0), global_time_offset_s=global_origin_s),
        _trace(
            "4p2v_cv",
            (3.0, near_three_s, 4.0),
            global_time_offset_s=global_origin_s,
        ),
    )

    bundle = build_charge_analysis_bundle(
        traces,
        cycle=10,
        mode="virtual",
        q_ref_ah=4.0,
        q_ref_node=0,
        q_ref_initial_ah=4.0,
        configured_charge_current_a=14.55,
        nominal_capacity_ah=4.85,
    )

    assert bundle.summary.values["external_charge_ah"] == pytest.approx(
        4.0 / 3600.0, abs=1e-14
    )
    assert bundle.summary.values["time_start_s"] == global_origin_s
    assert bundle.summary.values["time_end_s"] == global_origin_s + 4.0
    output_times = tuple(float(row["time_s"]) for row in bundle.trace_rows)
    assert all(right > left for left, right in zip(output_times, output_times[1:]))


def test_charge_analysis_collapses_only_zero_width_points_created_by_stage_rebasing() -> None:
    first_end_s = 10.919976585681004
    second_start_s = 10.919976585681006
    close_left_s = 89.66602386945912
    close_right_s = 89.66602386945914
    rebased = first_end_s + (
        np.asarray((close_left_s, close_right_s), dtype=float) - second_start_s
    )
    assert rebased[0] == rebased[1]

    traces = (
        _trace("3c_cc", (0.0, first_end_s), global_time_offset_s=0.0),
        _trace(
            "4v_cv",
            (second_start_s, close_left_s, close_right_s, 100.0),
            global_time_offset_s=0.0,
        ),
        _trace("c4_cc", (100.0, 101.0), global_time_offset_s=0.0),
        _trace("4p2v_cv", (101.0, 102.0), global_time_offset_s=0.0),
    )

    bundle = build_charge_analysis_bundle(
        traces,
        cycle=33,
        mode="virtual",
        q_ref_ah=4.0,
        q_ref_node=25,
        q_ref_initial_ah=4.0,
        configured_charge_current_a=14.55,
        nominal_capacity_ah=4.85,
    )

    output_times = tuple(float(row["time_s"]) for row in bundle.trace_rows)
    assert all(right > left for left, right in zip(output_times, output_times[1:]))
