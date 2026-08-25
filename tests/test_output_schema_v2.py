from __future__ import annotations

import csv

import pytest

from pybamm_w10.output import (
    CHARGE_EFFICIENCY_SOC_BIN_V3_FIELDS,
    CHARGE_EFFICIENCY_SUMMARY_V3_FIELDS,
    CHARGE_TIMESERIES_V3_FIELDS,
    append_charge_efficiency_summary,
    append_charge_soc_bins,
    append_dataclass,
    write_charge_timeseries,
)
from pybamm_w10.types import ChargeEfficiencySummary, ChargeSocBinResult, CycleResult, NumericalFailure, RPTResult


def _cycle() -> CycleResult:
    return CycleResult(
        cycle=1, mode="virtual", q_ref_ah=4.0, q_ref_node=0,
        step5_target_ah=0.8, window_target_ah=3.2, delta_q5_actual_ah=0.8,
        actual_udds_remaining_target_ah=2.4, udds_profile_available_ah=2.412,
        udds_guard_ah=0.012, udds_actual_ah=2.4, window_actual_ah=3.2,
        start_time_s=0.0, end_time_s=10.0,
        stage_durations_s={"step6_udds": 8.0},
        stage_wall_clock_durations_s={"step6_udds": 0.1},
    )


def test_cycle_schema_v3_writes_explicit_udds_and_wall_clock_fields(workspace_tmp) -> None:
    path = workspace_tmp / "cycle_summary.csv"
    append_dataclass(path, _cycle())
    with path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["output_schema_version"] == "3"
    assert row["actual_udds_remaining_target_ah"] == "2.4"
    assert row["udds_profile_available_ah"] == "2.412"
    assert row["udds_guard_ah"] == "0.012"
    assert row["udds_actual_ah"] == "2.4"
    assert row["wall_clock_step6_udds_s"] == "0.1"
    assert "udds_remaining_ah" not in row


def test_schema_v3_refuses_to_append_to_old_csv(workspace_tmp) -> None:
    path = workspace_tmp / "cycle_summary.csv"
    path.write_text("cycle,udds_remaining_ah\n1,2.4\n", encoding="utf-8")
    with pytest.raises(NumericalFailure, match="schema"):
        append_dataclass(path, _cycle())


def test_rpt_schema_uses_unambiguous_next_window_names(workspace_tmp) -> None:
    result = RPTResult(
        node=0, q_rpt_start_ah=0.0, q_rpt_end_ah=4.0, capacity_ah=4.0,
        soh_initial_pct=100.0, soh_nominal_pct=100.0, mode="virtual",
        start_time_s=0.0, end_time_s=1.0, diagnostic_duration_s=1.0,
        changed_main_state=False, main_state_hash_before="a", main_state_hash_after="a",
        main_time_before_s=0.0, main_time_after_s=0.0,
        main_capacity_before_ah=0.0, main_capacity_after_ah=0.0,
        became_q_ref=False,
    )
    path = workspace_tmp / "rpt_summary.csv"
    append_dataclass(path, result)
    header = path.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert {"next_step5_target_ah", "next_window_target_ah", "planned_udds_remaining_ah"} <= set(header)
    assert "next_udds_remaining_ah" not in header


def test_charge_schema_v3_has_fixed_headers_and_atomic_four_bin_group(workspace_tmp) -> None:
    summary_path = workspace_tmp / "charge_efficiency_summary.csv"
    bins_path = workspace_tmp / "charge_efficiency_soc_bins.csv"
    append_charge_efficiency_summary(
        summary_path,
        ChargeEfficiencySummary(1, "virtual", {"q_ref_ah": 4.0, "external_charge_ah": 3.2}),
    )
    bins = tuple(
        ChargeSocBinResult(1, "virtual", bin_id, {"q_ref_ah": 4.0})
        for bin_id in ("20-40", "40-60", "60-80", "80-100")
    )
    append_charge_soc_bins(bins_path, bins)
    assert tuple(summary_path.read_text(encoding="utf-8").splitlines()[0].split(",")) == CHARGE_EFFICIENCY_SUMMARY_V3_FIELDS
    assert tuple(bins_path.read_text(encoding="utf-8").splitlines()[0].split(",")) == CHARGE_EFFICIENCY_SOC_BIN_V3_FIELDS
    assert len(bins_path.read_text(encoding="utf-8").splitlines()) == 5


def test_charge_trace_requires_time_and_current_and_uses_fixed_header(workspace_tmp) -> None:
    path = workspace_tmp / "charge_timeseries" / "cycle-001.csv"
    artifact = write_charge_timeseries(path, (
        {"cycle": 1, "charge_stage": "3c_cc", "time_s": 0.0, "current_a": -14.55},
        {"cycle": 1, "charge_stage": "3c_cc", "time_s": 1.0, "current_a": -14.55},
    ))
    assert artifact.row_count == 2
    assert tuple(path.read_text(encoding="utf-8").splitlines()[0].split(",")) == CHARGE_TIMESERIES_V3_FIELDS
