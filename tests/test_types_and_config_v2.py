from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pybamm_w10.config import RunConfig
from pybamm_w10.types import (
    CapacityTargets,
    FailureContext,
    FailureReason,
    RPTResult,
    TerminationKind,
)


def test_v3_runtime_constants_are_serialized_and_fingerprinted() -> None:
    data_root = Path(r"E:\battery\data")
    config = RunConfig(data_root=data_root).normalized(Path(r"E:\SPMe"))

    assert config.protocol_algorithm_version == "w10-window-v3-charge-efficiency"
    assert config.output_schema_version == 3
    assert config.checkpoint_schema_version == 6
    assert config.udds_event_guard_fraction == pytest.approx(0.005)
    assert config.udds_event_guard_solver_steps == 10
    assert config.heartbeat_interval_s == 60
    assert config.checkpoint_every_cycles == 1
    assert config.data_root == data_root.resolve()
    assert config.w10_mat_path == data_root / "LG M50T/cycling/W10/W10-1.mat"
    assert config.cycling_root == data_root / "LG M50T/cycling"
    assert config.capacity_diagnostics_root == data_root / "LG M50T/_processed_mat"
    assert config.to_json()["data_root"] == str(data_root.resolve())
    assert replace(config, udds_event_guard_fraction=0.01).fingerprint() != config.fingerprint()


def test_failure_context_serializes_missing_values_as_json_null() -> None:
    context = FailureContext(reason=FailureReason.UNKNOWN_TERMINATION)

    assert context.to_json()["cycle"] is None
    assert context.to_json()["termination_kind"] is None
    assert context.to_json()["terminal_voltage_v"] is None
    with pytest.raises(ValueError):
        TerminationKind("not-a-termination")
    with pytest.raises(ValueError):
        FailureReason("not-a-reason")


def test_rpt_next_window_fields_are_explicit_and_formula_driven() -> None:
    targets = CapacityTargets(4.0, 0.8, 3.2, 2.4)
    result = RPTResult(
        node=25,
        q_rpt_start_ah=10.0,
        q_rpt_end_ah=14.0,
        capacity_ah=4.0,
        soh_initial_pct=100.0,
        soh_nominal_pct=100.0,
        mode="virtual",
        start_time_s=0.0,
        end_time_s=1.0,
        diagnostic_duration_s=1.0,
        changed_main_state=False,
        main_state_hash_before="before",
        main_state_hash_after="after",
        main_time_before_s=0.0,
        main_time_after_s=0.0,
        main_capacity_before_ah=0.0,
        main_capacity_after_ah=0.0,
        became_q_ref=True,
        targets=targets,
    )

    assert result.next_step5_target_ah == pytest.approx(0.8)
    assert result.next_window_target_ah == pytest.approx(3.2)
    assert result.planned_udds_remaining_ah == pytest.approx(2.4)
