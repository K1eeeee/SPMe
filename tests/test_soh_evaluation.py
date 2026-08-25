from __future__ import annotations

import csv
import json

import pytest

from pybamm_w10.calibration.data import W10_DIAGNOSTIC_NODES
from pybamm_w10.evaluation import (
    SohEvaluationError,
    build_soh_comparison,
    evaluate_soh_comparison,
)


def _write_experimental_diagnostics(root, capacities: dict[int, float]) -> None:
    folder = root / "LG M50T" / "_processed_mat"
    folder.mkdir(parents=True)
    header = "diagnostic_number,cell_id,time_s,current_a,voltage_v,capacity_ah\n"
    for index, node in enumerate(W10_DIAGNOSTIC_NODES, start=1):
        path = folder / f"W10_capacity_diagnostic_{index:02d}.csv"
        path.write_text(
            header
            + f"{index},W10,0,0.24,4.2,0\n"
            + f"{index},W10,100,0.24,2.5,{capacities[node]}\n",
            encoding="utf-8",
        )


def _write_run(run_dir, capacities: dict[int, float], *, status: str = "COMPLETED") -> None:
    run_dir.mkdir(parents=True)
    with (run_dir / "rpt_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("node", "capacity_ah"))
        writer.writeheader()
        writer.writerows(
            {"node": node, "capacity_ah": capacities[node]}
            for node in W10_DIAGNOSTIC_NODES
        )
    (run_dir / "run_status.json").write_text(
        json.dumps({"status": status}), encoding="utf-8"
    )
    (run_dir / "run_config.json").write_text("{}", encoding="utf-8")
    (run_dir / "effective_parameters.json").write_text("{}", encoding="utf-8")


def test_each_curve_is_normalised_by_its_own_cycle_zero_capacity() -> None:
    experimental = {node: 5.0 * (1.0 - node / 1000.0) for node in W10_DIAGNOSTIC_NODES}
    simulated = {node: 0.8 * experimental[node] for node in W10_DIAGNOSTIC_NODES}

    rows, metrics = build_soh_comparison(simulated, experimental)

    assert rows[0].simulated_soh_pct == pytest.approx(100.0)
    assert rows[0].experimental_soh_pct == pytest.approx(100.0)
    assert [row.signed_error_pp for row in rows] == pytest.approx([0.0] * len(rows))
    assert metrics.soh_mae_pp == pytest.approx(0.0)
    assert metrics.soh_rmse_pp == pytest.approx(0.0)
    assert metrics.soh_r_squared == pytest.approx(1.0)
    assert metrics.capacity_rmse_ah > 0


def test_soh_error_is_simulation_minus_experiment_in_percentage_points() -> None:
    experimental = {node: 5.0 * (1.0 - node / 1000.0) for node in W10_DIAGNOSTIC_NODES}
    simulated = {node: 4.0 * (1.0 - node / 2000.0) for node in W10_DIAGNOSTIC_NODES}

    rows, metrics = build_soh_comparison(simulated, experimental)

    assert rows[-1].cycle == 350
    assert rows[-1].simulated_soh_pct == pytest.approx(82.5)
    assert rows[-1].experimental_soh_pct == pytest.approx(65.0)
    assert rows[-1].signed_error_pp == pytest.approx(17.5)
    assert metrics.soh_final_error_pp == pytest.approx(17.5)
    assert metrics.soh_max_abs_error_pp == pytest.approx(17.5)


def test_evaluation_writes_comparison_metrics_audit_and_two_panel_figure(workspace_tmp) -> None:
    data_root = workspace_tmp / "data"
    run_dir = workspace_tmp / "run"
    experimental = {node: 5.0 - node / 1000.0 for node in W10_DIAGNOSTIC_NODES}
    simulated = {node: 0.9 * experimental[node] for node in W10_DIAGNOSTIC_NODES}
    _write_experimental_diagnostics(data_root, experimental)
    _write_run(run_dir, simulated)

    metrics = evaluate_soh_comparison(run_dir, data_root)

    assert metrics.node_count == 15
    assert metrics.soh_rmse_pp == pytest.approx(0.0)
    assert (run_dir / "soh_comparison.csv").is_file()
    assert (run_dir / "soh_accuracy.json").is_file()
    assert (run_dir / "figures" / "soh_sim_vs_experiment.png").is_file()
    payload = json.loads((run_dir / "soh_accuracy.json").read_text(encoding="utf-8"))
    assert payload["normalization"] == "each_curve_cycle_0_capacity"
    assert payload["error_definition"] == "simulated_soh_pct - experimental_soh_pct"
    assert len(payload["provenance"]["experimental_capacity_sha256"]) == 15


def test_evaluation_rejects_missing_rpt_node() -> None:
    experimental = {node: 5.0 for node in W10_DIAGNOSTIC_NODES}
    simulated = {node: 4.0 for node in W10_DIAGNOSTIC_NODES if node != 350}

    with pytest.raises(SohEvaluationError, match="exact W10 nodes"):
        build_soh_comparison(simulated, experimental)


def test_standalone_evaluation_rejects_incomplete_run(workspace_tmp) -> None:
    data_root = workspace_tmp / "data"
    run_dir = workspace_tmp / "run"
    capacities = {node: 5.0 - node / 1000.0 for node in W10_DIAGNOSTIC_NODES}
    _write_experimental_diagnostics(data_root, capacities)
    _write_run(run_dir, capacities, status="NUMERICAL_FAILURE")

    with pytest.raises(SohEvaluationError, match="COMPLETED"):
        evaluate_soh_comparison(run_dir, data_root)
