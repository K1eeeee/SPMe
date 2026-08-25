"""Post-run SOH comparison against the W10 capacity diagnostics."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .calibration.data import W10_DIAGNOSTIC_NODES, _read_capacity_endpoint
from .output import write_json


class SohEvaluationError(ValueError):
    """A completed run and the experimental capacity targets cannot be compared."""


@dataclass(frozen=True)
class SohComparisonRow:
    cycle: int
    simulated_capacity_ah: float
    experimental_capacity_ah: float
    simulated_soh_pct: float
    experimental_soh_pct: float
    signed_error_pp: float
    absolute_error_pp: float
    capacity_error_ah: float


@dataclass(frozen=True)
class SohAccuracyMetrics:
    node_count: int
    soh_mae_pp: float
    soh_rmse_pp: float
    soh_max_abs_error_pp: float
    soh_final_error_pp: float
    capacity_rmse_ah: float
    soh_r_squared: float | None


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _completed_status(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run_status.json"
    if not path.is_file():
        raise SohEvaluationError("standalone SOH evaluation requires run_status.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SohEvaluationError("run_status.json is not valid JSON") from exc
    if value.get("status") != "COMPLETED":
        raise SohEvaluationError("standalone SOH evaluation requires a COMPLETED run")
    return value


def _simulated_capacities(run_dir: Path) -> dict[int, float]:
    path = run_dir / "rpt_summary.csv"
    if not path.is_file():
        raise SohEvaluationError("SOH evaluation requires rpt_summary.csv")
    result: dict[int, float] = {}
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                node = int(row["node"])
                capacity = float(row["capacity_ah"])
                if node in result:
                    raise SohEvaluationError(f"duplicate simulated RPT node: {node}")
                if not math.isfinite(capacity) or capacity <= 0:
                    raise SohEvaluationError(f"invalid simulated capacity at node {node}: {capacity!r}")
                result[node] = capacity
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SohEvaluationError):
            raise
        raise SohEvaluationError("rpt_summary.csv has an invalid SOH evaluation schema") from exc
    expected = set(W10_DIAGNOSTIC_NODES)
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise SohEvaluationError(f"simulated RPT nodes do not match W10 schedule; missing={missing}, extra={extra}")
    return result


def _experimental_capacities(data_root: Path) -> tuple[dict[int, float], list[Path]]:
    folder = data_root.resolve() / "LG M50T" / "_processed_mat"
    paths = [folder / f"W10_capacity_diagnostic_{index:02d}.csv" for index in range(1, 16)]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SohEvaluationError(f"missing W10 capacity diagnostics: {missing}")
    try:
        values = {
            node: _read_capacity_endpoint(path)
            for node, path in zip(W10_DIAGNOSTIC_NODES, paths, strict=True)
        }
    except (OSError, ValueError) as exc:
        raise SohEvaluationError(f"invalid W10 capacity diagnostic: {exc}") from exc
    return values, paths


def build_soh_comparison(
    simulated_capacity_ah: dict[int, float],
    experimental_capacity_ah: dict[int, float],
) -> tuple[list[SohComparisonRow], SohAccuracyMetrics]:
    """Normalise each curve by its own cycle-0 capacity and compute errors."""
    expected = tuple(W10_DIAGNOSTIC_NODES)
    if set(simulated_capacity_ah) != set(expected):
        raise SohEvaluationError("simulated capacities do not contain the exact W10 nodes")
    if set(experimental_capacity_ah) != set(expected):
        raise SohEvaluationError("experimental capacities do not contain the exact W10 nodes")
    q_sim_0 = float(simulated_capacity_ah[0])
    q_exp_0 = float(experimental_capacity_ah[0])
    if not all(math.isfinite(value) and value > 0 for value in (q_sim_0, q_exp_0)):
        raise SohEvaluationError("cycle-0 capacities must be finite and positive")

    rows: list[SohComparisonRow] = []
    for node in expected:
        q_sim = float(simulated_capacity_ah[node])
        q_exp = float(experimental_capacity_ah[node])
        if not all(math.isfinite(value) and value > 0 for value in (q_sim, q_exp)):
            raise SohEvaluationError(f"capacities at node {node} must be finite and positive")
        sim_soh = 100.0 * q_sim / q_sim_0
        exp_soh = 100.0 * q_exp / q_exp_0
        signed_error = sim_soh - exp_soh
        rows.append(
            SohComparisonRow(
                cycle=node,
                simulated_capacity_ah=q_sim,
                experimental_capacity_ah=q_exp,
                simulated_soh_pct=sim_soh,
                experimental_soh_pct=exp_soh,
                signed_error_pp=signed_error,
                absolute_error_pp=abs(signed_error),
                capacity_error_ah=q_sim - q_exp,
            )
        )

    errors = np.asarray([row.signed_error_pp for row in rows], dtype=float)
    capacity_errors = np.asarray([row.capacity_error_ah for row in rows], dtype=float)
    experimental_soh = np.asarray([row.experimental_soh_pct for row in rows], dtype=float)
    residual_sum = float(np.sum(errors**2))
    total_sum = float(np.sum((experimental_soh - np.mean(experimental_soh)) ** 2))
    r_squared = None if total_sum == 0 else 1.0 - residual_sum / total_sum
    metrics = SohAccuracyMetrics(
        node_count=len(rows),
        soh_mae_pp=float(np.mean(np.abs(errors))),
        soh_rmse_pp=float(np.sqrt(np.mean(errors**2))),
        soh_max_abs_error_pp=float(np.max(np.abs(errors))),
        soh_final_error_pp=float(errors[-1]),
        capacity_rmse_ah=float(np.sqrt(np.mean(capacity_errors**2))),
        soh_r_squared=r_squared,
    )
    return rows, metrics


def _write_comparison_csv(path: Path, rows: list[SohComparisonRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(rows[0]))
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(asdict(row) for row in rows)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _plot_comparison(path: Path, rows: list[SohComparisonRow]) -> None:
    import matplotlib.pyplot as plt

    cycles = [row.cycle for row in rows]
    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(7.5, 7.0), height_ratios=(2, 1))
    axes[0].plot(cycles, [row.simulated_soh_pct for row in rows], "o-", label="PyBaMM SPMe")
    axes[0].plot(cycles, [row.experimental_soh_pct for row in rows], "x--", label="Experiment")
    axes[0].set_ylabel("SOH [%]")
    axes[0].legend(loc="best")
    axes[0].grid(alpha=0.25)
    axes[1].axhline(0.0, color="black", linewidth=0.8)
    axes[1].plot(cycles, [row.signed_error_pp for row in rows], "o-", color="tab:red")
    axes[1].set(xlabel="Aging cycle", ylabel="Error [pp]")
    axes[1].grid(alpha=0.25)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def evaluate_soh_comparison(
    run_dir: Path,
    data_root: Path,
    *,
    require_completed_status: bool = True,
) -> SohAccuracyMetrics:
    """Evaluate a complete W10 RPT series and write auditable artifacts."""
    run_dir = run_dir.resolve()
    if require_completed_status:
        _completed_status(run_dir)
    simulated = _simulated_capacities(run_dir)
    experimental, experimental_paths = _experimental_capacities(data_root)
    rows, metrics = build_soh_comparison(simulated, experimental)

    comparison_path = run_dir / "soh_comparison.csv"
    metrics_path = run_dir / "soh_accuracy.json"
    figure_path = run_dir / "figures" / "soh_sim_vs_experiment.png"
    _write_comparison_csv(comparison_path, rows)
    run_config_path = run_dir / "run_config.json"
    effective_parameters_path = run_dir / "effective_parameters.json"
    payload: dict[str, Any] = {
        "evaluation_schema_version": 1,
        "normalization": "each_curve_cycle_0_capacity",
        "error_definition": "simulated_soh_pct - experimental_soh_pct",
        "error_unit": "percentage_points",
        "metrics": asdict(metrics),
        "artifacts": {
            "comparison_csv": comparison_path.name,
            "figure": figure_path.relative_to(run_dir).as_posix(),
        },
        "provenance": {
            "rpt_summary_sha256": _sha256_file(run_dir / "rpt_summary.csv"),
            "run_config_sha256": _sha256_file(run_config_path) if run_config_path.is_file() else None,
            "effective_parameters_sha256": (
                _sha256_file(effective_parameters_path) if effective_parameters_path.is_file() else None
            ),
            "experimental_capacity_sha256": {
                path.name: _sha256_file(path) for path in experimental_paths
            },
            "evaluation_stage": (
                "completed_run" if require_completed_status else "post_cycles_pre_terminal_commit"
            ),
        },
    }
    write_json(metrics_path, payload)
    _plot_comparison(figure_path, rows)
    return metrics
