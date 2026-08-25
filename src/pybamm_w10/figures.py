"""Post-run figures.  This module is never imported by a dry-run command."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _experimental_capacity(w10_mat_path: Path) -> tuple[list[int], list[float]]:
    from .config import RPT_NODES

    folder = w10_mat_path.parents[2] / "_processed_mat"
    capacities: list[float] = []
    for path in sorted(folder.glob("W10_capacity_diagnostic_*.csv")):
        rows = _rows(path)
        values = [float(row["capacity_ah"]) for row in rows if row.get("capacity_ah")]
        if values:
            capacities.append(max(values))
    return list(RPT_NODES[: len(capacities)]), capacities


def _completed_mode_runs(output_root: Path) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for candidate in output_root.iterdir() if output_root.exists() else ():
        if not candidate.is_dir():
            continue
        status_path, config_path = candidate / "run_status.json", candidate / "run_config.json"
        if not status_path.is_file() or not config_path.is_file() or not (candidate / "rpt_summary.csv").is_file():
            continue
        status = json.loads(status_path.read_text(encoding="utf-8"))
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if status.get("status") != "COMPLETED" or status.get("smoke"):
            continue
        mode = config.get("mode")
        if mode in {"virtual", "strict-w10"}:
            previous = selected.get(mode)
            if previous is None or candidate.stat().st_mtime > previous.stat().st_mtime:
                selected[mode] = candidate
    return selected


def plot_degradation_contributions(axes, rows: list[dict[str, str]]) -> None:
    """Plot only the schema-2 degradation columns with their physical units."""
    if not rows:
        return
    x = [float(row["cycle"]) for row in rows]
    keys = (
        "sei_loss_ah",
        "plating_loss_ah",
        "dead_lithium_loss_ah",
        "negative_lam_pct",
        "positive_lam_pct",
    )
    for key in keys:
        if key in rows[0]:
            axes.plot(x, [float(row[key]) for row in rows], label=key)


def generate_figures(
    run_dir: Path,
    w10_mat_path: Path | None = None,
    comparison_root: Path | None = None,
) -> None:
    """Create the capacity/SOH, temperature, degradation, and trace plots on completion."""
    import matplotlib.pyplot as plt

    figure_dir = run_dir / "figures"
    figure_dir.mkdir(exist_ok=True)
    rpt = _rows(run_dir / "rpt_summary.csv")
    cycles = _rows(run_dir / "cycle_summary.csv")
    degradation = _rows(run_dir / "degradation_summary.csv")

    if rpt:
        x = [float(row["node"]) for row in rpt]
        q = [float(row["capacity_ah"]) for row in rpt]
        soh = [float(row["soh_nominal_pct"]) for row in rpt]
        fig, left = plt.subplots()
        left.plot(x, q, "o-", label="SPMe RPT capacity")
        if w10_mat_path is not None:
            measured_x, measured_q = _experimental_capacity(w10_mat_path)
            if measured_q:
                left.plot(measured_x, measured_q, "x--", label="W10 measured (not fitted)")
        left.legend(loc="best")
        left.set(xlabel="Aging cycle", ylabel="0.24 A capacity [Ah]")
        right = left.twinx(); right.plot(x, soh, "s--", color="tab:orange", label="Nominal SOH")
        right.set_ylabel("SOH [%]")
        fig.tight_layout(); fig.savefig(figure_dir / "capacity_soh.png", dpi=160); plt.close(fig)

    if cycles:
        x = [float(row["cycle"]) for row in cycles]
        temperatures = [
            float(row["temperature_max_k"]) if row.get("temperature_max_k") else None
            for row in cycles
        ]
        if any(value is not None for value in temperatures):
            fig, ax = plt.subplots(); ax.plot(x, temperatures, "o-")
            ax.set(xlabel="Aging cycle", ylabel="End temperature [K]")
            fig.tight_layout(); fig.savefig(figure_dir / "temperature.png", dpi=160); plt.close(fig)

    if degradation:
        fig, ax = plt.subplots()
        plot_degradation_contributions(ax, degradation)
        ax.set(xlabel="Aging cycle", ylabel="Degradation metric"); ax.legend()
        fig.tight_layout(); fig.savefig(figure_dir / "degradation_contributions.png", dpi=160); plt.close(fig)

    for trace_path in sorted((run_dir / "timeseries").glob("cycle-*.csv")):
        trace = _rows(trace_path)
        if not trace or "current_a" not in trace:
            continue
        t = [float(row["time_s"]) for row in trace]
        fig, axes = plt.subplots(3, 1, sharex=True, figsize=(7, 6))
        axes[0].plot(t, [float(row["current_a"]) for row in trace]); axes[0].set_ylabel("I [A]")
        if "terminal_voltage_v" in trace[0]: axes[1].plot(t, [float(row["terminal_voltage_v"]) for row in trace])
        axes[1].set_ylabel("V [V]")
        if "temperature_k" in trace[0]: axes[2].plot(t, [float(row["temperature_k"]) for row in trace])
        axes[2].set(xlabel="Time [s]", ylabel="T [K]")
        fig.tight_layout(); fig.savefig(figure_dir / f"{trace_path.stem}.png", dpi=160); plt.close(fig)

    if comparison_root is not None:
        modes = _completed_mode_runs(comparison_root)
        current_config = run_dir / "run_config.json"
        if current_config.is_file() and (run_dir / "rpt_summary.csv").is_file():
            current_mode = json.loads(current_config.read_text(encoding="utf-8")).get("mode")
            if current_mode in {"virtual", "strict-w10"}:
                modes[current_mode] = run_dir
        if set(modes) == {"virtual", "strict-w10"}:
            fig, ax = plt.subplots()
            for mode, path in modes.items():
                rows = _rows(path / "rpt_summary.csv")
                ax.plot(
                    [float(row["node"]) for row in rows],
                    [float(row["capacity_ah"]) for row in rows],
                    "o-",
                    label=mode,
                )
            ax.set(xlabel="Aging cycle", ylabel="RPT capacity [Ah]")
            ax.legend()
            fig.tight_layout()
            fig.savefig(figure_dir / "virtual_strict_comparison.png", dpi=160)
            plt.close(fig)
