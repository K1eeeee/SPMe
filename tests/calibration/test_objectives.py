from __future__ import annotations

import numpy as np
import pytest

from pybamm_w10.calibration.objectives import (
    CandidateScore,
    CAPACITY_TARGET_AH,
    ObjectiveError,
    capacity_objective,
    rank_candidates,
    soh_metrics,
    voltage_curve_metrics,
)


def test_capacity_and_voltage_objectives_use_fixed_targets_and_grid() -> None:
    capacity = capacity_objective(CAPACITY_TARGET_AH * 1.001)
    assert capacity.target_ah == CAPACITY_TARGET_AH
    assert capacity.relative_error == pytest.approx(0.001)
    assert capacity.passed

    exp_capacity = np.array([0.0, 0.2, 0.2, 0.5, 1.0])
    exp_voltage = np.array([4.2, 3.86, 3.86, 3.35, 2.5])
    sim_capacity = np.array([0.0, 0.5, 1.0])
    sim_voltage = np.array([4.21, 3.36, 2.51])
    metrics = voltage_curve_metrics(sim_capacity, sim_voltage, exp_capacity, exp_voltage)

    assert len(metrics.normalized_capacity_grid) == 1001
    assert metrics.full_rmse_v == pytest.approx(0.01)
    assert metrics.mid_rmse_v == pytest.approx(0.01)
    assert metrics.max_abs_error_v == pytest.approx(0.01)
    assert metrics.status == "CAPACITY_MATCHED_VOLTAGE_PASSED"


def test_voltage_objective_labels_failure_and_rejects_nonmonotonic_input() -> None:
    metrics = voltage_curve_metrics(
        np.array([0.0, 1.0]), np.array([4.26, 2.56]),
        np.array([0.0, 1.0]), np.array([4.2, 2.5]),
    )
    assert metrics.status == "CAPACITY_MATCHED_VOLTAGE_FAILED"
    with pytest.raises(ObjectiveError, match="non-monotonic"):
        voltage_curve_metrics(
            np.array([0.0, 0.8, 0.4]), np.array([4.2, 3.5, 2.5]),
            np.array([0.0, 1.0]), np.array([4.2, 2.5]),
        )


def test_soh_metrics_normalise_both_cycle_zero_capacities_and_rank_deterministically() -> None:
    targets = {0: 5.0, 25: 4.5, 75: 4.0}
    metrics = soh_metrics({0: 10.0, 25: 9.0, 75: 8.0}, targets, (0, 25, 75))
    assert metrics.rmse_pp == pytest.approx(0.0)
    # Near-equal RMSE uses max error, then endpoint error rather than input order.
    worse_peak = soh_metrics({0: 10.0, 25: 8.98, 75: 8.0}, targets, (0, 25, 75))
    ordered = rank_candidates(
        [
            CandidateScore("B", worse_peak, (0.0, 0.0, 0.0)),
            CandidateScore("A", metrics, (0.0, 0.0, 0.0)),
            CandidateScore("C", None, (0.0, 0.0, 0.0), numerically_censored=True),
        ]
    )
    assert [item.candidate_id for item in ordered] == ["A", "B"]


def test_soh_metrics_use_cycle_zero_only_as_the_normalisation_anchor() -> None:
    metrics = soh_metrics(
        {0: 10.0, 25: 8.8, 75: 7.8},
        {0: 5.0, 25: 4.5, 75: 4.0},
        (0, 25, 75),
    )

    assert [node.signed_error_pp for node in metrics.nodes] == pytest.approx([0.0, -2.0, -2.0])
    assert metrics.rmse_pp == pytest.approx(2.0)
    assert metrics.max_absolute_error_pp == pytest.approx(2.0)
