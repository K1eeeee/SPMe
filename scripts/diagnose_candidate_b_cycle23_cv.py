"""Diagnose the candidate-B cycle-23 4.0 V CV solver failure.

This script is intentionally read-only with respect to the production run.  It
loads the cycle-022 checkpoint, reruns a two-stage 3C-CC/4.0-V-CV prefix with
selected IDAKLU BDF orders, and writes a standalone JSON diagnostic artifact.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import pickle
import sys
import time
import traceback
from typing import Any

import numpy as np


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "src"))

from pybamm_w10.backend import PyBaMMBackend
from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.config import RunConfig
from pybamm_w10.model import (
    build_solver,
    build_spme,
    certified_charge_solver_profile,
    conservative_charge_solver_profile,
)


CANDIDATE_DIR = (
    WORKSPACE
    / "outputs"
    / "pybamm_spme_calibration"
    / "w10-stage1-soh-v1"
    / "candidates"
    / "B"
)
CHECKPOINT_PATH = CANDIDATE_DIR / "checkpoints" / "cycle-022.pkl"
OUTPUT_PATH = CANDIDATE_DIR / "diagnostics" / "cycle-023-4v-cv-deep-diagnostic.json"


def _config() -> RunConfig:
    saved = json.loads((CANDIDATE_DIR / "run_config.json").read_text(encoding="utf-8"))
    return replace(
        RunConfig(
            mode=saved["mode"],
            data_root=Path(saved["data_root"]),
            output_root=CANDIDATE_DIR,
            calibration_parameters_path=CANDIDATE_DIR / "candidate_parameters.json",
        ),
        run_context_fingerprint=saved["run_context_fingerprint"],
    ).normalized(WORKSPACE)


def _finite_stats(solution: Any, name: str, times: np.ndarray) -> dict[str, Any]:
    try:
        values = np.asarray(solution[name](times), dtype=float)
    except Exception as exc:  # diagnostic collection must not hide the solve
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"available": True, "finite": False, "shape": list(values.shape)}
    flattened = np.ravel(values)
    return {
        "available": True,
        "finite": bool(np.all(np.isfinite(values))),
        "shape": list(values.shape),
        "minimum": float(np.min(finite)),
        "maximum": float(np.max(finite)),
        "start": float(flattened[0]),
        "end": float(flattened[-1]),
    }


VARIABLES = (
    "Current [A]",
    "Terminal voltage [V]",
    "X-averaged cell temperature [K]",
    "X-averaged negative electrode porosity",
    "Minimum negative particle surface concentration [mol.m-3]",
    "Maximum negative particle surface concentration [mol.m-3]",
    "Negative electrolyte concentration [mol.m-3]",
    "Separator electrolyte concentration [mol.m-3]",
    "Positive electrolyte concentration [mol.m-3]",
    "X-averaged negative electrode lithium plating reaction overpotential [V]",
    "X-averaged negative electrode lithium plating interfacial current density [A.m-2]",
    "X-averaged negative electrode interfacial current density [A.m-2]",
    "X-averaged negative electrode SEI interfacial current density [A.m-2]",
    "Volume-averaged negative lithium plating concentration [mol.m-3]",
    "Loss of capacity to negative lithium plating [A.h]",
    "Volume-averaged negative SEI thickness [m]",
)


def _experiment(config: RunConfig, cv_duration_s: float | None) -> Any:
    import pybamm

    cv_kwargs: dict[str, Any] = {"termination": f"{config.protocol.cv_cutoff_a} A"}
    if cv_duration_s is not None:
        cv_kwargs["duration"] = float(cv_duration_s)
    return pybamm.Experiment(
        [(
            pybamm.step.current(-config.protocol.charge_3c_a, termination="4.0 V"),
            pybamm.step.voltage(4.0, **cv_kwargs),
        )]
    )


def _run_prefix(
    artifacts: Any,
    config: RunConfig,
    checkpoint: Any,
    profile: Any,
    cv_duration_s: float | None,
    *,
    collect_variables: bool = False,
) -> dict[str, Any]:
    import pybamm

    solver = build_solver(config, profile)
    solver._options["print_stats"] = True
    simulation = pybamm.Simulation(
        artifacts.model,
        parameter_values=artifacts.parameter_values,
        solver=solver,
        experiment=_experiment(config, cv_duration_s),
    )
    started = time.perf_counter()
    try:
        solution = simulation.solve(
            starting_solution=checkpoint.state.solution,
            showprogress=False,
        )
    except Exception as exc:
        return {
            "status": "FAILED",
            "profile": asdict(profile),
            "requested_cv_duration_s": cv_duration_s,
            "wall_clock_s": time.perf_counter() - started,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }

    cycle = solution.cycles[-1]
    steps = tuple(cycle.steps)
    output: dict[str, Any] = {
        "status": "COMPLETED" if len(steps) == 2 else "PARTIAL_FAILURE",
        "profile": asdict(profile),
        "requested_cv_duration_s": cv_duration_s,
        "wall_clock_s": time.perf_counter() - started,
        "step_count": len(steps),
        "step_durations_s": [float(step.t[-1] - step.t[0]) for step in steps],
        "step_terminations": [str(step.termination) for step in steps],
        "step_point_counts": [int(len(step.t)) for step in steps],
    }
    if len(steps) >= 2:
        cv = steps[1]
        terminal_time = np.asarray([float(cv.t[-1])])
        output["cv_terminal_variables"] = {
            name: _finite_stats(cv, name, terminal_time) for name in VARIABLES
        }
    if len(steps) >= 2 and collect_variables:
        cv_times = np.unique(
            np.concatenate(
                ([float(cv.t[0])], np.linspace(float(cv.t[0]), float(cv.t[-1]), 1001))
            )
        )
        output["cv_time_origin_s"] = float(cv.t[0])
        output["cv_time_end_s"] = float(cv.t[-1])
        output["cv_variables"] = {
            name: _finite_stats(cv, name, cv_times) for name in VARIABLES
        }
        dense_offsets = np.linspace(0.0, min(700.0, float(cv.t[-1] - cv.t[0])), 2801)
        dense_times = float(cv.t[0]) + dense_offsets
        dense_variables = {
            "current_a": np.asarray(cv["Current [A]"](dense_times), dtype=float),
            "voltage_v": np.asarray(cv["Terminal voltage [V]"](dense_times), dtype=float),
            "temperature_k": np.asarray(
                cv["X-averaged cell temperature [K]"](dense_times), dtype=float
            ),
            "negative_electrolyte_concentration": np.asarray(
                cv["Negative electrolyte concentration [mol.m-3]"](dense_times),
                dtype=float,
            ),
            "negative_surface_concentration": np.asarray(
                cv["Negative particle surface concentration [mol.m-3]"](dense_times),
                dtype=float,
            ),
            "plating_overpotential_v": np.asarray(
                cv[
                    "X-averaged negative electrode lithium plating reaction overpotential [V]"
                ](dense_times),
                dtype=float,
            ),
            "plating_current_density_a_m2": np.asarray(
                cv[
                    "X-averaged negative electrode lithium plating interfacial current density [A.m-2]"
                ](dense_times),
                dtype=float,
            ),
            "intercalation_current_density_a_m2": np.asarray(
                cv["X-averaged negative electrode interfacial current density [A.m-2]"](
                    dense_times
                ),
                dtype=float,
            ),
            "plating_concentration_mol_m3": np.asarray(
                cv["Volume-averaged negative lithium plating concentration [mol.m-3]"](
                    dense_times
                ),
                dtype=float,
            ),
        }

        def reduce_space(values: np.ndarray, operation: str) -> np.ndarray:
            if values.ndim <= 1:
                return np.ravel(values)
            axes = tuple(range(values.ndim - 1))
            return getattr(np, operation)(values, axis=axes)

        reduced = {
            "current_a": reduce_space(dense_variables["current_a"], "min"),
            "voltage_v": reduce_space(dense_variables["voltage_v"], "max"),
            "temperature_k": reduce_space(dense_variables["temperature_k"], "max"),
            "negative_electrolyte_min_mol_m3": reduce_space(
                dense_variables["negative_electrolyte_concentration"], "min"
            ),
            "negative_surface_concentration_min_mol_m3": reduce_space(
                dense_variables["negative_surface_concentration"], "min"
            ),
            "plating_overpotential_v": reduce_space(
                dense_variables["plating_overpotential_v"], "min"
            ),
            "plating_current_density_a_m2": reduce_space(
                dense_variables["plating_current_density_a_m2"], "min"
            ),
            "intercalation_current_density_a_m2": reduce_space(
                dense_variables["intercalation_current_density_a_m2"], "min"
            ),
            "plating_concentration_mol_m3": reduce_space(
                dense_variables["plating_concentration_mol_m3"], "min"
            ),
        }
        output["dense_0_to_700s_extrema"] = {}
        for name, values in reduced.items():
            minimum_index = int(np.nanargmin(values))
            maximum_index = int(np.nanargmax(values))
            output["dense_0_to_700s_extrema"][name] = {
                "minimum": float(values[minimum_index]),
                "minimum_time_from_cv_start_s": float(dense_offsets[minimum_index]),
                "maximum": float(values[maximum_index]),
                "maximum_time_from_cv_start_s": float(dense_offsets[maximum_index]),
            }
        electrolyte_minimum = reduced["negative_electrolyte_min_mol_m3"]
        nonpositive = np.flatnonzero(electrolyte_minimum <= 0.0)
        output["negative_electrolyte_nonpositive_interval_s"] = (
            None
            if nonpositive.size == 0
            else [float(dense_offsets[nonpositive[0]]), float(dense_offsets[nonpositive[-1]])]
        )
        landmarks = (
            0.0,
            0.01,
            0.1,
            1.0,
            10.0,
            100.0,
            400.0,
            480.0,
            490.0,
            500.0,
            501.0,
            501.269045,
            502.0,
            505.0,
            505.782511,
            506.0,
            510.0,
            520.0,
            600.0,
            700.0,
        )
        output["cv_landmarks"] = {}
        for offset in landmarks:
            index = int(np.argmin(np.abs(dense_offsets - offset)))
            output["cv_landmarks"][f"{offset:.6f}"] = {
                name: float(values[index]) for name, values in reduced.items()
            }
        for offset_s in (0.0, 1e-8, 1e-6, 1e-4, 1e-2, 1.0, 10.0):
            point = min(float(cv.t[0]) + offset_s, float(cv.t[-1]))
            output.setdefault("transition_samples", {})[f"{offset_s:.9g}"] = {
                name: _finite_stats(cv, name, np.asarray([point]))
                for name in VARIABLES[:2] + VARIABLES[8:13]
            }
    return output


def main() -> int:
    config = _config()
    parameters = load_calibration_parameters(CANDIDATE_DIR / "candidate_parameters.json")
    artifacts = build_spme(config, parameters)
    with CHECKPOINT_PATH.open("rb") as handle:
        checkpoint = pickle.load(handle)

    primary = certified_charge_solver_profile(config)
    retry = conservative_charge_solver_profile(config)
    bdf1 = replace(
        retry,
        name="diagnostic_bdf1",
        max_order_bdf=1,
        max_error_test_failures=200,
    )
    report: dict[str, Any] = {
        "diagnostic_only": True,
        "candidate": "B",
        "source_checkpoint": str(CHECKPOINT_PATH),
        "source_aging_cycle": checkpoint.aging_cycle,
        "source_state_hash": checkpoint.state.state_hash,
        "checkpoint_time_s": checkpoint.state.time_s,
        "runs": [],
    }

    # The passing BDF1 trajectory supplies a physically interpretable reference.
    report["runs"].append(
        _run_prefix(artifacts, config, checkpoint, bdf1, None, collect_variables=True)
    )

    # Scan logarithmically near the transition, then across the full reference CV.
    horizons = (100.0, 400.0, 480.0, 490.0, 500.0, 501.0, 501.2, 501.3,
                502.0, 505.0, 505.7, 505.8, 506.0, 510.0, 520.0)
    for profile in (primary, retry, bdf1):
        for horizon in horizons:
            report["runs"].append(
                _run_prefix(artifacts, config, checkpoint, profile, horizon)
            )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
