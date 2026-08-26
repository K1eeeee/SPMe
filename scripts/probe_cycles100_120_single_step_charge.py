"""Replay cycles 100--120 charge from checkpoints with four single-step Experiments.

This is diagnostic-only: each step receives the preceding solve's terminal
solution as ``starting_solution``.  It never commits a solution or writes to
the source run.
"""

from __future__ import annotations

import argparse
import gc
import pickle
from pathlib import Path
import sys
import traceback

import pybamm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.config import RunConfig
from pybamm_w10.model import build_spme
from pybamm_w10.output import write_json


def _steps(config: RunConfig) -> tuple[tuple[str, object], ...]:
    protocol = config.protocol
    return (
        ("3c_cc", pybamm.step.current(-protocol.charge_3c_a, termination="4.0 V")),
        ("4v_cv", pybamm.step.voltage(4.0, termination=f"{protocol.cv_cutoff_a} A")),
        (
            "c4_cc",
            pybamm.step.current(
                -protocol.discharge_c4_a,
                termination=f"{config.cell.upper_cutoff_v} V",
            ),
        ),
        (
            "4p2v_cv",
            pybamm.step.voltage(
                config.cell.upper_cutoff_v,
                termination=f"{protocol.cv_cutoff_a} A",
            ),
        ),
    )


def _run_cycle(config: RunConfig, calibration, checkpoint_path: Path) -> dict[str, object]:
    with checkpoint_path.open("rb") as handle:
        checkpoint = pickle.load(handle)
    artifacts = build_spme(config, calibration)
    starting_solution = checkpoint.state.solution
    results: list[dict[str, object]] = []
    for solve_number, (stage, step) in enumerate(_steps(config), start=1):
        simulation = pybamm.Simulation(
            artifacts.model,
            parameter_values=artifacts.parameter_values,
            solver=artifacts.charge_solver,
            experiment=pybamm.Experiment([step]),
        )
        try:
            solution = simulation.solve(
                starting_solution=starting_solution,
                showprogress=False,
            )
        except Exception as exc:
            results.append({
                "solve_number": solve_number,
                "stage": stage,
                "status": "FAILED",
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            })
            return {
                "cycle": checkpoint.aging_cycle + 1,
                "source_checkpoint": str(checkpoint_path),
                "source_state_hash": checkpoint.state.state_hash,
                "status": "FAILED",
                "first_solve_failed": solve_number == 1,
                "solves": results,
            }
        results.append({
            "solve_number": solve_number,
            "stage": stage,
            "status": "COMPLETED",
            "termination": str(solution.termination),
            "point_count": len(solution.t),
            "end_time_s": float(solution.t[-1]),
        })
        starting_solution = solution
    return {
        "cycle": checkpoint.aging_cycle + 1,
        "source_checkpoint": str(checkpoint_path),
        "source_state_hash": checkpoint.state.state_hash,
        "status": "COMPLETED",
        "first_solve_failed": False,
        "solves": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--calibration-params", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    source_run = args.source_run.resolve()
    output = args.output.resolve()
    if workspace not in output.parents:
        raise ValueError("diagnostic output must remain inside the workspace")
    config = RunConfig(
        mode="virtual",
        data_root=args.data_root.resolve(),
        calibration_parameters_path=args.calibration_params.resolve(),
    ).normalized(workspace)
    calibration = load_calibration_parameters(config.calibration_parameters_path)
    records: list[dict[str, object]] = []
    for cycle in range(100, 121):
        checkpoint = source_run / "checkpoints" / f"cycle-{cycle - 1:03d}.pkl"
        if not checkpoint.is_file():
            raise FileNotFoundError(f"missing source checkpoint for cycle {cycle}: {checkpoint}")
        record = _run_cycle(config, calibration, checkpoint)
        records.append(record)
        gc.collect()
    failures = [record for record in records if record["status"] != "COMPLETED"]
    first_solve_failures = [record["cycle"] for record in records if record["first_solve_failed"]]
    write_json(output, {
        "diagnostic_only": True,
        "method": "four_separate_simulation_solves",
        "cycles": [100, 120],
        "source_run": str(source_run),
        "solver": "artifacts.charge_solver (certified charge profile)",
        "records": records,
        "summary": {
            "cycles_tested": len(records),
            "completed_cycles": len(records) - len(failures),
            "failed_cycles": [record["cycle"] for record in failures],
            "first_solve_failures": first_solve_failures,
        },
    })
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
