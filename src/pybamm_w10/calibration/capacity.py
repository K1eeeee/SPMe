"""Strict-W10 cycle-0 capacity-factor calibration only.

This module deliberately owns a narrow solve surface: every evaluator call
constructs a fresh SPMe from the canonical 20% SOC state and runs one RPT.  It
does not import the aging protocol or runner.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
from typing import Callable

import numpy as np

from ..backend import PyBaMMBackend, construct_initial_state_record
from ..config import RunConfig
from ..diagnostics import run_capacity_rpt
from ..model import build_spme, effective_parameters_audit
from ..output import RunDirectoryBusy, RunDirectoryLock, append_log
from .artifacts import write_calibration_csv, write_calibration_json
from .data import build_diagnostic_inventory, load_cycle0_capacity_curve, write_diagnostic_inventory
from .objectives import CAPACITY_RELATIVE_TOLERANCE, capacity_objective, voltage_curve_metrics
from .parameters import (
    CAPACITY_SCALE_BOUNDS,
    CalibrationParameters,
)
from .workflow import CalibrationState, CalibrationWorkflow


CAPACITY_SCALE_INTERVAL_TOLERANCE = 1e-4
CAPACITY_SEARCH_MAX_EVALUATIONS = 16
CAPACITY_REPEAT_MAX_RELATIVE_DIFFERENCE = 0.0002


class CapacityCalibrationError(RuntimeError):
    """The fixed, approved capacity search cannot produce a valid artifact."""


@dataclass(frozen=True)
class CapacityCandidate:
    """One independently solved cycle-0 candidate and its discharge curve."""

    scale_factor: float
    capacity_ah: float
    discharge_capacity_ah: np.ndarray
    voltage_v: np.ndarray
    initial_state_fingerprint: str
    parameter_fingerprint: str

    @property
    def relative_error(self) -> float:
        return capacity_objective(self.capacity_ah).relative_error

    def to_json(self) -> dict[str, object]:
        return {
            "scale_factor": self.scale_factor,
            "capacity_ah": self.capacity_ah,
            "relative_error": self.relative_error,
            "initial_state_fingerprint": self.initial_state_fingerprint,
            "parameter_fingerprint": self.parameter_fingerprint,
            "curve_points": int(self.discharge_capacity_ah.size),
        }


@dataclass(frozen=True)
class CapacitySearchResult:
    candidates: tuple[CapacityCandidate, ...]
    winner: CapacityCandidate
    repeated_winner: CapacityCandidate
    bracket_width: float
    repeat_relative_difference: float
    converged: bool


CandidateSolver = Callable[[float], CapacityCandidate]
CandidateObserver = Callable[[int, str, CapacityCandidate], None]


def _extract_rpt_discharge_curve(
    trace: dict[str, object], q_start_ah: float, q_end_ah: float
) -> tuple[np.ndarray, np.ndarray]:
    """Select only the positive-current capacity-discharge segment of an RPT."""
    try:
        global_capacity = np.asarray(trace["discharge_capacity_ah"], dtype=float)
        current = np.asarray(trace["current_a"], dtype=float)
        voltage = np.asarray(trace["terminal_voltage_v"], dtype=float)
    except KeyError as exc:
        raise CapacityCalibrationError("cycle-0 RPT did not return a voltage/current/capacity trace") from exc
    mask = (
        (current > 0)
        & (global_capacity >= q_start_ah - 1e-10)
        & (global_capacity <= q_end_ah + 1e-10)
    )
    discharge_capacity = global_capacity[mask] - q_start_ah
    discharge_voltage = voltage[mask]
    if discharge_capacity.size < 2:
        raise CapacityCalibrationError("cycle-0 RPT has no complete positive-current discharge trace")
    return discharge_capacity, discharge_voltage


def _validate_candidate(candidate: CapacityCandidate, expected_scale: float) -> None:
    if not math.isclose(candidate.scale_factor, expected_scale, rel_tol=0.0, abs_tol=1e-12):
        raise CapacityCalibrationError("candidate evaluator returned a mismatched scale factor")
    if not math.isfinite(candidate.capacity_ah) or candidate.capacity_ah <= 0:
        raise CapacityCalibrationError("candidate capacity must be finite and positive")
    if candidate.discharge_capacity_ah.ndim != 1 or candidate.voltage_v.ndim != 1:
        raise CapacityCalibrationError("candidate voltage curve must be one-dimensional")
    if candidate.discharge_capacity_ah.size < 2 or candidate.discharge_capacity_ah.size != candidate.voltage_v.size:
        raise CapacityCalibrationError("candidate voltage curve is incomplete")
    if not np.all(np.isfinite(candidate.discharge_capacity_ah)) or not np.all(np.isfinite(candidate.voltage_v)):
        raise CapacityCalibrationError("candidate voltage curve contains NaN or Inf")
    if np.any(np.diff(candidate.discharge_capacity_ah) < 0):
        raise CapacityCalibrationError("candidate discharge capacity curve is non-monotonic")
    if len(candidate.initial_state_fingerprint) == 0 or len(candidate.parameter_fingerprint) == 0:
        raise CapacityCalibrationError("candidate is missing independent-state audit fingerprints")


def run_capacity_search(
    solve_candidate: CandidateSolver,
    *,
    observer: CandidateObserver | None = None,
) -> CapacitySearchResult:
    """Deterministically bisect the approved electrode-width interval.

    The 16-evaluation budget includes the two endpoints and the independent
    repeat.  No adaptive widening, alternate parameter, or hidden fallback is
    permitted.
    """
    lower_scale, upper_scale = CAPACITY_SCALE_BOUNDS
    candidates: list[CapacityCandidate] = []

    def evaluate(scale: float, role: str) -> CapacityCandidate:
        # Keep one slot for the mandatory fresh repeat.
        if len(candidates) >= CAPACITY_SEARCH_MAX_EVALUATIONS - 1:
            raise CapacityCalibrationError("capacity search exhausted its fixed 16-evaluation budget")
        candidate = solve_candidate(scale)
        _validate_candidate(candidate, scale)
        candidates.append(candidate)
        if observer is not None:
            observer(len(candidates), role, candidate)
        return candidate

    lower = evaluate(lower_scale, "lower_bound")
    upper = evaluate(upper_scale, "upper_bound")
    if not upper.capacity_ah > lower.capacity_ah:
        raise CapacityCalibrationError("capacity response is not strictly monotonic over the approved bracket")
    target = capacity_objective(lower.capacity_ah).target_ah
    if not lower.capacity_ah <= target <= upper.capacity_ah:
        raise CapacityCalibrationError("approved capacity-scale bracket does not contain the cycle-0 target")

    best = min(candidates, key=lambda candidate: candidate.relative_error)
    converged = False
    while True:
        bracket_width = upper.scale_factor - lower.scale_factor
        if best.relative_error <= CAPACITY_RELATIVE_TOLERANCE and bracket_width <= CAPACITY_SCALE_INTERVAL_TOLERANCE:
            converged = True
            break
        midpoint = (lower.scale_factor + upper.scale_factor) / 2
        midpoint_candidate = evaluate(midpoint, "bisection")
        if midpoint_candidate.relative_error < best.relative_error:
            best = midpoint_candidate
        if midpoint_candidate.capacity_ah < target:
            lower = midpoint_candidate
        else:
            upper = midpoint_candidate

    repeated = solve_candidate(best.scale_factor)
    _validate_candidate(repeated, best.scale_factor)
    if len(candidates) + 1 > CAPACITY_SEARCH_MAX_EVALUATIONS:
        raise CapacityCalibrationError("independent repeat would exceed the fixed evaluation budget")
    if observer is not None:
        observer(len(candidates) + 1, "independent_repeat", repeated)
    repeat_difference = abs(repeated.capacity_ah - best.capacity_ah) / best.capacity_ah
    if repeat_difference > CAPACITY_REPEAT_MAX_RELATIVE_DIFFERENCE:
        raise CapacityCalibrationError(
            "independent winner repeat exceeds the 0.02% capacity reproducibility limit"
        )
    return CapacitySearchResult(
        candidates=tuple(candidates),
        winner=best,
        repeated_winner=repeated,
        bracket_width=upper.scale_factor - lower.scale_factor,
        repeat_relative_difference=repeat_difference,
        converged=converged,
    )


def solve_cycle0_candidate(config: RunConfig, scale_factor: float) -> CapacityCandidate:
    """Build a new strict-W10 SPMe and solve only its isolated cycle-0 RPT."""
    if config.mode != "strict-w10":
        raise CapacityCalibrationError("cycle-0 capacity calibration requires strict-w10 mode")
    parameters = CalibrationParameters(
        calibration_status="CAPACITY_CALIBRATED",
        capacity_scale_factor=scale_factor,
    )
    artifacts = build_spme(config, parameters)
    initial_state = construct_initial_state_record(artifacts, config)
    backend = PyBaMMBackend(artifacts, config.initial_soc, initial_state)
    rpt = run_capacity_rpt(backend, 0, config, initial_capacity_ah=None, virtual=False)
    discharge_capacity, discharge_voltage = _extract_rpt_discharge_curve(
        rpt.timeseries, rpt.q_rpt_start_ah, rpt.q_rpt_end_ah
    )
    return CapacityCandidate(
        scale_factor=scale_factor,
        capacity_ah=rpt.capacity_ah,
        discharge_capacity_ah=discharge_capacity,
        voltage_v=discharge_voltage,
        initial_state_fingerprint=initial_state.fingerprint,
        parameter_fingerprint=parameters.fingerprint,
    )


def _write_candidate(output_dir: Path, index: int, role: str, candidate: CapacityCandidate) -> None:
    candidate_dir = output_dir / "candidates" / f"candidate-{index:03d}"
    candidate_dir.mkdir(parents=True, exist_ok=False)
    write_calibration_json(candidate_dir / "status.json", {"role": role, "status": "SOLVED", **candidate.to_json()})
    append_log(
        candidate_dir / "run.log",
        f"role={role} scale={candidate.scale_factor:.8f} capacity_ah={candidate.capacity_ah:.12g} "
        f"initial_state_fingerprint={candidate.initial_state_fingerprint} "
        f"parameter_fingerprint={candidate.parameter_fingerprint}",
    )
    write_calibration_csv(
        candidate_dir / "cycle0_discharge_curve.csv",
        ("capacity_ah", "voltage_v"),
        [
            {"capacity_ah": float(capacity), "voltage_v": float(voltage)}
            for capacity, voltage in zip(candidate.discharge_capacity_ah, candidate.voltage_v, strict=True)
        ],
    )


def _write_voltage_artifacts(
    output_dir: Path,
    result: CapacitySearchResult,
    experimental_capacity: np.ndarray,
    experimental_voltage: np.ndarray,
) -> dict[str, object]:
    metrics = voltage_curve_metrics(
        result.repeated_winner.discharge_capacity_ah,
        result.repeated_winner.voltage_v,
        experimental_capacity,
        experimental_voltage,
    )
    rows = [
        {
            "normalized_capacity": float(x),
            "simulated_voltage_v": float(simulated),
            "experimental_voltage_v": float(experimental),
            "absolute_error_v": float(abs(simulated - experimental)),
        }
        for x, simulated, experimental in zip(
            metrics.normalized_capacity_grid,
            metrics.simulated_voltage_v,
            metrics.experimental_voltage_v,
            strict=True,
        )
    ]
    write_calibration_csv(
        output_dir / "voltage_curve_comparison.csv",
        ("normalized_capacity", "simulated_voltage_v", "experimental_voltage_v", "absolute_error_v"),
        rows,
    )
    try:
        import matplotlib.pyplot as plt

        figure_dir = output_dir / "figures"
        figure_dir.mkdir(exist_ok=True)
        fig, axis = plt.subplots()
        axis.plot(metrics.normalized_capacity_grid, metrics.experimental_voltage_v, label="W10 cycle-0 experimental")
        axis.plot(metrics.normalized_capacity_grid, metrics.simulated_voltage_v, label="SPMe calibrated")
        axis.set(xlabel="Normalized discharged capacity", ylabel="Voltage [V]")
        axis.legend(loc="best")
        fig.tight_layout()
        fig.savefig(figure_dir / "cycle0_voltage_comparison.png", dpi=160)
        plt.close(fig)
    except ImportError:  # pragma: no cover - matplotlib is a declared runtime dependency
        pass
    return {
        "status": metrics.status,
        "full_rmse_v": metrics.full_rmse_v,
        "mid_rmse_v": metrics.mid_rmse_v,
        "max_abs_error_v": metrics.max_abs_error_v,
        "endpoint_capacity_relative_error": metrics.endpoint_capacity_relative_error,
    }


def run_capacity_calibration(
    config: RunConfig,
    output_dir: Path,
    *,
    candidate_solver: CandidateSolver | None = None,
    inventory_builder: Callable[[Path], dict[str, object]] = build_diagnostic_inventory,
    experimental_curve_loader: Callable[[Path], tuple[np.ndarray, np.ndarray]] = load_cycle0_capacity_curve,
) -> CapacitySearchResult:
    """Execute the approved isolated cycle-0 calibration and write its artifacts."""
    if config.mode != "strict-w10":
        raise CapacityCalibrationError("--calibrate-capacity requires --mode strict-w10")
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise CapacityCalibrationError(f"refusing to overwrite existing calibration output: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    solver = candidate_solver or (lambda scale: solve_cycle0_candidate(config, scale))
    try:
        with RunDirectoryLock(
            output_dir,
            {"kind": "cycle-0-capacity-calibration", "mode": config.mode, "config_fingerprint": config.fingerprint()},
        ) as lock:
            for child in ("checkpoints", "figures", "candidates"):
                (output_dir / child).mkdir(exist_ok=True)
            write_calibration_json(
                output_dir / "calibration_config.json",
                {
                    "kind": "cycle-0-capacity-calibration",
                    "mode": config.mode,
                    "initial_soc": config.initial_soc,
                    "config": config.to_json(),
                    "capacity_scale_bounds": list(CAPACITY_SCALE_BOUNDS),
                    "capacity_scale_interval_tolerance": CAPACITY_SCALE_INTERVAL_TOLERANCE,
                    "capacity_relative_tolerance": CAPACITY_RELATIVE_TOLERANCE,
                    "capacity_search_max_evaluations": CAPACITY_SEARCH_MAX_EVALUATIONS,
                    "capacity_repeat_max_relative_difference": CAPACITY_REPEAT_MAX_RELATIVE_DIFFERENCE,
                },
            )
            inventory = inventory_builder(config.data_root)
            write_diagnostic_inventory(output_dir / "diagnostic_inventory.json", inventory)
            initial_parameters = CalibrationParameters()
            workflow = CalibrationWorkflow(output_dir, initial_parameters.fingerprint)
            workflow.transition(CalibrationState.CAPACITY_CALIBRATION_READY)
            append_log(output_dir / "run.log", "cycle-0 capacity search started; aging scheduling is disabled")

            def observe(index: int, role: str, candidate: CapacityCandidate) -> None:
                _write_candidate(output_dir, index, role, candidate)
                append_log(
                    output_dir / "run.log",
                    f"candidate={index:03d} role={role} scale={candidate.scale_factor:.8f} capacity_ah={candidate.capacity_ah:.12g}",
                )

            result = run_capacity_search(solver, observer=observe)
            write_calibration_csv(
                output_dir / "capacity_search.csv",
                ("candidate", "scale_factor", "capacity_ah", "relative_error", "initial_state_fingerprint", "parameter_fingerprint"),
                [
                    {
                        "candidate": index,
                        "scale_factor": candidate.scale_factor,
                        "capacity_ah": candidate.capacity_ah,
                        "relative_error": candidate.relative_error,
                        "initial_state_fingerprint": candidate.initial_state_fingerprint,
                        "parameter_fingerprint": candidate.parameter_fingerprint,
                    }
                    for index, candidate in enumerate((*result.candidates, result.repeated_winner), start=1)
                ],
            )
            final_parameters = replace(
                initial_parameters,
                calibration_status="CAPACITY_CALIBRATED",
                capacity_scale_factor=result.winner.scale_factor,
            )
            experimental_capacity, experimental_voltage = experimental_curve_loader(config.data_root)
            voltage = _write_voltage_artifacts(output_dir, result, experimental_capacity, experimental_voltage)
            capacity_result = {
                "state": "CAPACITY_CALIBRATED",
                "target_capacity_ah": capacity_objective(result.winner.capacity_ah).target_ah,
                "scale_factor": result.winner.scale_factor,
                "capacity_ah": result.winner.capacity_ah,
                "relative_error": result.winner.relative_error,
                "bracket_width": result.bracket_width,
                "repeat_capacity_ah": result.repeated_winner.capacity_ah,
                "repeat_relative_difference": result.repeat_relative_difference,
                "candidate_evaluations": len(result.candidates) + 1,
                "voltage": voltage,
                "holdout_accessed": False,
            }
            workflow.parameter_fingerprint = final_parameters.fingerprint
            workflow.record_capacity_result(capacity_result)
            artifacts = build_spme(config, final_parameters)
            write_calibration_json(
                output_dir / "effective_parameters.json",
                effective_parameters_audit(
                    artifacts,
                    config,
                    cycle_0_capacity_ah=result.winner.capacity_ah,
                    calibration_parameters=final_parameters,
                ),
            )
            write_calibration_json(output_dir / "calibrated_parameters.json", final_parameters.to_json())
            workflow.apply_aging_data_gate(dict(inventory["aging_calibration_gate"]))
            write_calibration_json(
                output_dir / "checkpoints" / "capacity-search-committed.json",
                {"resume_eligible": False, "kind": "cycle-0-capacity-audit", "candidate_evaluations": len(result.candidates) + 1},
            )
            append_log(output_dir / "run.log", "cycle-0 capacity calibration completed; no aging cycle was run")
            lock.set_business_status("CAPACITY_CALIBRATED")
            return result
    except RunDirectoryBusy:
        raise
    except Exception as exc:
        # Preserve all independently committed candidate evidence without
        # pretending that a partially completed search is resumable.
        write_calibration_json(
            output_dir / "calibration_failure.json",
            {"status": "FAILED", "reason": type(exc).__name__, "error": str(exc), "resume_eligible": False},
        )
        raise
