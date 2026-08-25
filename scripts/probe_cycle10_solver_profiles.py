"""Diagnostic-only IDAKLU option probe at the legacy cycle-9 boundary."""

from __future__ import annotations

import argparse
import gc
import pickle
from pathlib import Path
import traceback

import pybamm

from pybamm_w10.backend import build_standard_charge_experiment
from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.config import RunConfig
from pybamm_w10.model import build_spme
from pybamm_w10.output import write_json


PROFILES = (
    {
        "name": "suppress_algebraic_error",
        "dt_init": 1e-8,
        "max_error_test_failures": 30,
        "max_order_bdf": 3,
        "suppress_algebraic_error": True,
    },
    {
        "name": "smaller_initial_step",
        "dt_init": 1e-10,
        "max_error_test_failures": 100,
        "max_order_bdf": 2,
        "suppress_algebraic_error": False,
    },
    {
        "name": "all_initial_conditions",
        "dt_init": 1e-8,
        "max_error_test_failures": 50,
        "max_order_bdf": 3,
        "suppress_algebraic_error": False,
        "init_all_y_ic": True,
    },
)


def run_profile(config, calibration, state, profile):
    artifacts = build_spme(config, calibration)
    options = {
        "max_num_steps": config.solver.max_num_steps,
        "dt_init": profile["dt_init"],
        "dt_max": config.solver.max_step_s,
        "max_error_test_failures": profile["max_error_test_failures"],
        "max_order_bdf": profile["max_order_bdf"],
        "suppress_algebraic_error": profile["suppress_algebraic_error"],
    }
    if "init_all_y_ic" in profile:
        options["init_all_y_ic"] = profile["init_all_y_ic"]
    solver = pybamm.IDAKLUSolver(
        rtol=config.solver.rtol,
        atol=config.solver.atol,
        root_tol=config.solver.root_tol,
        on_failure="error",
        on_extrapolation="error",
        options=options,
    )
    captured = {"error": None, "step": None}

    class ProbeCallback(pybamm.callbacks.Callback):
        def on_step_start(self, logs):
            captured["step"] = int(logs["step number"][0]) - 1

        def on_experiment_error(self, logs):
            captured["error"] = str(logs.get("error"))

    simulation = pybamm.Simulation(
        artifacts.model,
        parameter_values=artifacts.parameter_values,
        solver=solver,
        experiment=build_standard_charge_experiment(config),
    )
    try:
        solution = simulation.solve(
            starting_solution=state.solution,
            showprogress=False,
            callbacks=ProbeCallback(),
        )
        steps = tuple(solution.cycles[-1].steps)
        if len(steps) != 4:
            raise RuntimeError(f"incomplete charge sequence: {len(steps)} steps; {captured['error']}")
    except Exception as exc:
        return {
            "name": profile["name"],
            "status": "FAILED",
            "step": captured["step"],
            "callback_error": captured["error"],
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    return {
        "name": profile["name"],
        "status": "COMPLETED",
        "step_count": len(steps),
        "termination": [str(step.termination) for step in steps],
        "end_time_s": float(solution.t[-1]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--calibration-params", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = args.output.resolve()
    if workspace not in output.parents:
        raise ValueError("probe output must remain inside workspace")
    config = RunConfig(
        mode="virtual",
        data_root=Path(r"E:\battery\data"),
        calibration_parameters_path=args.calibration_params,
    ).normalized(workspace)
    calibration = load_calibration_parameters(config.calibration_parameters_path)
    with args.checkpoint.resolve().open("rb") as handle:
        checkpoint = pickle.load(handle)
    results = []
    for profile in PROFILES:
        results.append(run_profile(config, calibration, checkpoint.state, profile))
        gc.collect()
        if results[-1]["status"] == "COMPLETED":
            break
    write_json(output, {
        "diagnostic_only": True,
        "source_checkpoint": str(args.checkpoint.resolve()),
        "profiles": results,
    })
    return 0 if any(result["status"] == "COMPLETED" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
