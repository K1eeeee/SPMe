"""Replay only cycles 1--122 standard charges with one fixed solver profile.

Each charge starts independently from the preceding checkpoint of an existing
completed run.  The source checkpoints and run artifacts are read-only: no
candidate state is committed and no RPT, rest, Step 5, UDDS, or aging advance
is executed.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, replace
import gc
import json
from math import sqrt
from pathlib import Path
import pickle
import sys
from time import monotonic
import traceback
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pybamm_w10.backend import PyBaMMBackend, STANDARD_CHARGE_STAGE_NAMES
from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.charge_efficiency import build_charge_analysis_bundle
from pybamm_w10.charge_variables import preflight_charge_variables
from pybamm_w10.config import RunConfig
from pybamm_w10.model import build_spme, certified_charge_solver_profile
from pybamm_w10.output import append_json_line, write_json
from pybamm_w10.runner import ensure_required_interpreter
from pybamm_w10.types import Checkpoint, SolverStepFailure


FIXED_PROFILE_NAME = "fixed_conservative_standard_charge"
EXPECTED_TERMINATIONS = ("VOLTAGE", "CURRENT", "VOLTAGE", "CURRENT")
SUMMARY_METRICS = (
    "duration_s",
    "external_charge_ah",
    "cc_charge_ah",
    "cv_charge_ah",
    "soc_at_charge_end_pct",
    "intercalated_charge_increment_ah",
    "reversible_plating_increment_ah",
    "dead_lithium_increment_ah",
    "sei_increment_ah",
    "useful_charge_efficiency_pct",
    "reversible_retention_pct",
    "charge_balance_abs_error_pct",
    "negative_electrode_min_potential_v",
)
CURVE_METRICS = (
    "terminal_voltage_v",
    "temperature_k",
    "reference_soc_pct",
    "negative_particle_lithium_mol",
    "total_plating_inventory_ah",
    "cumulative_sei_loss_ah",
)


def fixed_conservative_profile(config: RunConfig):
    """Return exactly the user-requested IDAKLU standard-charge profile."""
    return replace(certified_charge_solver_profile(config), name=FIXED_PROFILE_NAME)


def checkpoint_for_charge(source_run: Path, cycle: int) -> Path:
    if not 1 <= cycle <= 122:
        raise ValueError("charge replay cycle must be in [1, 122]")
    return source_run / "checkpoints" / f"cycle-{cycle - 1:03d}.pkl"


def _read_csv_by_cycle(path: Path) -> dict[int, dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {int(row["cycle"]): row for row in csv.DictReader(handle)}


def _read_attempts(path: Path) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                records[int(record["cycle"])] = record
    return records


def _float(row: dict[str, str], name: str) -> float | None:
    value = row.get(name, "")
    return None if value in (None, "") else float(value)


def _baseline_trace(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _curve_differences(
    replay_rows: tuple[dict[str, object], ...], baseline_rows: list[dict[str, str]]
) -> dict[str, float]:
    differences = {f"curve_max_abs_delta_{name}": 0.0 for name in CURVE_METRICS}
    for stage in STANDARD_CHARGE_STAGE_NAMES:
        replay = [row for row in replay_rows if row["charge_stage"] == stage]
        baseline = [row for row in baseline_rows if row["charge_stage"] == stage]
        if len(replay) < 2 or len(baseline) < 2:
            raise ValueError(f"insufficient trace points for {stage}")
        replay_time = np.asarray([float(row["time_s"]) for row in replay])
        baseline_time = np.asarray([float(row["time_s"]) for row in baseline])
        replay_phase = (replay_time - replay_time[0]) / (replay_time[-1] - replay_time[0])
        baseline_phase = (baseline_time - baseline_time[0]) / (baseline_time[-1] - baseline_time[0])
        grid = np.linspace(0.0, 1.0, 201)
        for name in CURVE_METRICS:
            replay_value = np.asarray([float(row[name]) for row in replay])
            baseline_value = np.asarray([float(row[name]) for row in baseline])
            delta = np.max(
                np.abs(
                    np.interp(grid, replay_phase, replay_value)
                    - np.interp(grid, baseline_phase, baseline_value)
                )
            )
            key = f"curve_max_abs_delta_{name}"
            differences[key] = max(differences[key], float(delta))
    return differences


def _branch_reasons(record: dict[str, Any]) -> list[str]:
    if record["status"] != "COMPLETED":
        return ["solver_failure"]
    reasons: list[str] = []
    if tuple(record["termination_kinds"]) != EXPECTED_TERMINATIONS:
        reasons.append("termination_mismatch")
    for stage in STANDARD_CHARGE_STAGE_NAMES:
        absolute = abs(float(record[f"delta_duration_{stage}_s"]))
        baseline = abs(float(record[f"baseline_duration_{stage}_s"]))
        if absolute > 1.0 and absolute / max(baseline, 1.0) > 0.005:
            reasons.append(f"{stage}_duration_shift")
    thresholds = {
        "delta_external_charge_ah": 5e-4,
        "delta_intercalated_charge_increment_ah": 5e-4,
        "delta_soc_at_charge_end_pct": 2e-2,
        "delta_useful_charge_efficiency_pct": 2e-2,
        "curve_max_abs_delta_terminal_voltage_v": 5e-3,
        "curve_max_abs_delta_temperature_k": 2e-1,
    }
    for name, threshold in thresholds.items():
        if abs(float(record[name])) > threshold:
            reasons.append(name)
    return reasons


def _distribution(records: list[dict[str, Any]], name: str) -> dict[str, Any]:
    values = np.asarray([float(record[name]) for record in records], dtype=float)
    order = np.argsort(np.abs(values))[::-1][:5]
    return {
        "mean": float(np.mean(values)),
        "rms": float(sqrt(float(np.mean(values**2)))),
        "p95_abs": float(np.percentile(np.abs(values), 95)),
        "max_abs": float(np.max(np.abs(values))),
        "max_abs_cycle": int(records[int(np.argmax(np.abs(values)))]["cycle"]),
        "largest": [
            {"cycle": int(records[int(index)]["cycle"]), "value": float(values[index])}
            for index in order
        ],
    }


def _final_summary(records: list[dict[str, Any]], elapsed_s: float) -> dict[str, Any]:
    completed = [record for record in records if record["status"] == "COMPLETED"]
    failed = [record for record in records if record["status"] != "COMPLETED"]
    delta_names = [f"delta_{name}" for name in SUMMARY_METRICS]
    delta_names.extend(f"delta_duration_{stage}_s" for stage in STANDARD_CHARGE_STAGE_NAMES)
    delta_names.extend(f"curve_max_abs_delta_{name}" for name in CURVE_METRICS)
    return {
        "status": "COMPLETED" if not failed else "COMPLETED_WITH_FAILURES",
        "cycles_requested": len(records),
        "cycles_completed": len(completed),
        "failed_cycles": [int(record["cycle"]) for record in failed],
        "branch_flagged_cycles": [
            int(record["cycle"]) for record in records if record.get("possible_branch_reasons")
        ],
        "branch_flags": {
            str(record["cycle"]): record["possible_branch_reasons"]
            for record in records
            if record.get("possible_branch_reasons")
        },
        "elapsed_wall_clock_s": elapsed_s,
        "comparison_distributions": (
            {name: _distribution(completed, name) for name in delta_names}
            if completed
            else {}
        ),
    }


def _write_flat_csv(path: Path, records: list[dict[str, Any]]) -> None:
    rows = []
    for record in records:
        row = {
            key: json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, (dict, list, tuple))
            else value
            for key, value in record.items()
            if key != "traceback"
        }
        rows.append(row)
    names = sorted({name for row in rows for name in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--calibration-params", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cycle-start", type=int, default=1)
    parser.add_argument("--cycle-end", type=int, default=122)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    source_run = args.source_run.resolve()
    output_dir = args.output_dir.resolve()
    if not (1 <= args.cycle_start <= args.cycle_end <= 122):
        raise ValueError("cycle range must be contained in [1, 122]")
    if workspace not in output_dir.parents:
        raise ValueError("diagnostic output must remain inside the workspace")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty output: {output_dir}")
    source_status = json.loads((source_run / "run_status.json").read_text(encoding="utf-8"))
    if source_status.get("status") != "COMPLETED" or source_status.get("completed_aging_cycles") != 122:
        raise ValueError("source run must be a completed 122-cycle run")

    base_config = RunConfig(
        mode="virtual",
        data_root=args.data_root.resolve(),
        calibration_parameters_path=args.calibration_params.resolve(),
    )
    config = replace(
        base_config,
        protocol=replace(
            base_config.protocol,
            max_aging_cycles=122,
            rpt_nodes=(0, 25, 75, 122),
        ),
    ).normalized(workspace)
    ensure_required_interpreter(config)
    profile = fixed_conservative_profile(config)
    calibration = load_calibration_parameters(config.calibration_parameters_path)
    baseline_summary = _read_csv_by_cycle(source_run / "charge_efficiency_summary.csv")
    baseline_cycles = _read_csv_by_cycle(source_run / "cycle_summary.csv")
    baseline_attempts = _read_attempts(source_run / "solver_attempts.jsonl")

    checkpoints = [checkpoint_for_charge(source_run, cycle) for cycle in range(args.cycle_start, args.cycle_end + 1)]
    missing = [str(path) for path in checkpoints if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing source checkpoints: {missing}")
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "audit.json", {
        "diagnostic_only": True,
        "read_only_checkpoint_replay": True,
        "source_run": str(source_run),
        "cycle_range": [args.cycle_start, args.cycle_end],
        "excluded_phases": ["RPT", "post_charge_rest", "Step 5", "UDDS", "aging_state_commit"],
        "solver_profile": asdict(profile),
        "root_tol": config.solver.root_tol,
        "physical_protocol_unchanged": True,
        "source_config_fingerprint": json.loads(
            (source_run / "cycle122_validation_scope.json").read_text(encoding="utf-8")
        )["config_fingerprint"],
    })

    artifacts = build_spme(config, calibration)
    inventory = preflight_charge_variables(artifacts.model, model_options=artifacts.options)
    records: list[dict[str, Any]] = []
    started = monotonic()
    for cycle, checkpoint_path in zip(range(args.cycle_start, args.cycle_end + 1), checkpoints, strict=True):
        cycle_started = monotonic()
        record: dict[str, Any] = {
            "cycle": cycle,
            "status": "FAILED",
            "source_checkpoint": str(checkpoint_path),
            "solver_profile": profile.name,
        }
        try:
            with checkpoint_path.open("rb") as handle:
                checkpoint = pickle.load(handle)
            if not isinstance(checkpoint, Checkpoint) or checkpoint.aging_cycle != cycle - 1:
                raise ValueError("checkpoint/cycle mapping is invalid")
            if checkpoint.config_fingerprint != config.fingerprint():
                raise ValueError("checkpoint configuration fingerprint differs from replay configuration")
            if checkpoint.q_ref_ah is None or checkpoint.q_ref_node is None or checkpoint.initial_capacity_ah is None:
                raise ValueError("checkpoint lacks charge capacity references")

            backend = PyBaMMBackend(artifacts, config.initial_soc)
            backend.restore(checkpoint.state)
            pre_charge = backend.snapshot()
            result = backend._solve_standard_charge_attempt(
                config, pre_charge, profile, inventory, None
            )
            if backend.snapshot().state_hash != pre_charge.state_hash:
                raise AssertionError("read-only replay unexpectedly committed candidate state")
            analysis = build_charge_analysis_bundle(
                result.traces,
                cycle=cycle,
                mode=config.mode,
                q_ref_ah=float(checkpoint.q_ref_ah),
                q_ref_node=int(checkpoint.q_ref_node),
                q_ref_initial_ah=float(checkpoint.initial_capacity_ah),
                configured_charge_current_a=config.protocol.charge_3c_a,
                nominal_capacity_ah=config.cell.nominal_capacity_ah,
                faraday_constant_c_per_mol=config.faraday_constant_c_per_mol,
                soc_anchor_pct=config.soc_anchor_pct,
                balance_pass_limit_pct=config.charge_balance_pass_limit_pct,
                balance_failure_limit_pct=config.charge_balance_failure_limit_pct,
            )
            baseline = baseline_summary[cycle]
            baseline_cycle = baseline_cycles[cycle]
            replay_values = analysis.summary.values
            record.update({
                "status": "COMPLETED",
                "source_state_hash": pre_charge.state_hash,
                "candidate_terminal_state_hash": result.terminal_snapshot.state_hash,
                "termination_kinds": [item.termination_kind.value for item in result.outcomes],
                "raw_terminations": [item.raw_termination for item in result.outcomes],
                "baseline_solver_profile": baseline_attempts[cycle]["solver_profile"],
                "baseline_attempt_count": baseline_attempts[cycle]["attempt_count"],
                "analysis_primary_status": analysis.status.primary_status.value,
                "analysis_status_flags": [item.value for item in analysis.status.status_flags],
                "trace_point_count": len(analysis.trace_rows),
            })
            for name in SUMMARY_METRICS:
                replay_value = float(replay_values[name])
                baseline_value = _float(baseline, name)
                if baseline_value is None:
                    raise ValueError(f"baseline metric is missing: {name}")
                record[name] = replay_value
                record[f"baseline_{name}"] = baseline_value
                record[f"delta_{name}"] = replay_value - baseline_value
            for stage in STANDARD_CHARGE_STAGE_NAMES:
                replay_duration = float(result.stage_durations_s[stage])
                baseline_duration = float(baseline_cycle[f"duration_{stage}_s"])
                record[f"duration_{stage}_s"] = replay_duration
                record[f"baseline_duration_{stage}_s"] = baseline_duration
                record[f"delta_duration_{stage}_s"] = replay_duration - baseline_duration
            record.update(_curve_differences(
                analysis.trace_rows,
                _baseline_trace(source_run / "charge_timeseries" / f"cycle-{cycle:03d}.csv"),
            ))
            record["possible_branch_reasons"] = _branch_reasons(record)
        except Exception as exc:
            record.update({
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
                "possible_branch_reasons": ["solver_failure"],
            })
            if isinstance(exc, SolverStepFailure):
                record.update({
                    "sundials_error_code": exc.sundials_error_code,
                    "failed_step_index": exc.failed_step_index,
                    "charge_stage": exc.charge_stage,
                })
        record["wall_clock_s"] = monotonic() - cycle_started
        records.append(record)
        append_json_line(output_dir / "replay_records.jsonl", record)
        write_json(output_dir / "progress.json", {
            "cycles_completed": len(records),
            "current_cycle": cycle,
            "failed_cycles": [item["cycle"] for item in records if item["status"] != "COMPLETED"],
            "branch_flagged_cycles": [item["cycle"] for item in records if item.get("possible_branch_reasons")],
            "elapsed_wall_clock_s": monotonic() - started,
        })
        del record
        for name in ("analysis", "result", "backend", "checkpoint"):
            if name in locals():
                del locals()[name]
        gc.collect()

    summary = _final_summary(records, monotonic() - started)
    write_json(output_dir / "summary.json", summary)
    _write_flat_csv(output_dir / "replay_summary.csv", records)
    write_json(output_dir / "progress.json", {**summary, "finished": True})
    return 0 if not summary["failed_cycles"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
