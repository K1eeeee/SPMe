"""Deterministic cycle-0 capacity and voltage objectives without any solver calls."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

import numpy as np


CAPACITY_TARGET_AH = 4.865884391243259
CAPACITY_RELATIVE_TOLERANCE = 0.002
VOLTAGE_GRID_POINTS = 1001
VOLTAGE_FULL_RMSE_LIMIT_V = 0.050
CALIBRATION_RMSE_LIMIT_PP = 1.0
HOLDOUT_RMSE_LIMIT_PP = 3.0
CYCLE_350_ABS_LIMIT_PP = 4.0


class ObjectiveError(ValueError):
    """A proposed objective curve cannot be compared reproducibly."""


@dataclass(frozen=True)
class CapacityObjective:
    target_ah: float
    simulated_capacity_ah: float
    absolute_error_ah: float
    relative_error: float
    passed: bool


@dataclass(frozen=True)
class VoltageCurveMetrics:
    full_rmse_v: float
    mid_rmse_v: float
    max_abs_error_v: float
    endpoint_capacity_relative_error: float
    status: str
    normalized_capacity_grid: np.ndarray
    simulated_voltage_v: np.ndarray
    experimental_voltage_v: np.ndarray


@dataclass(frozen=True)
class SohNodeMetric:
    cycle: int
    simulated_capacity_ah: float
    experimental_capacity_ah: float
    simulated_soh_pct: float
    experimental_soh_pct: float
    signed_error_pp: float
    absolute_error_pp: float


@dataclass(frozen=True)
class SohMetrics:
    nodes: tuple[SohNodeMetric, ...]
    rmse_pp: float
    max_absolute_error_pp: float
    endpoint_absolute_error_pp: float


@dataclass(frozen=True)
class Stage1Acceptance:
    calibration_passed: bool
    holdout_passed: bool | None
    cycle_350_passed: bool | None


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    metrics: SohMetrics | None
    log10_scales: tuple[float, float, float]
    retry_count: int = 0
    numerically_censored: bool = False


def capacity_objective(
    simulated_capacity_ah: float,
    target_ah: float = CAPACITY_TARGET_AH,
) -> CapacityObjective:
    if not all(math.isfinite(value) and value > 0 for value in (simulated_capacity_ah, target_ah)):
        raise ObjectiveError("capacity objective requires finite positive capacities")
    absolute_error = simulated_capacity_ah - target_ah
    relative_error = abs(absolute_error) / target_ah
    return CapacityObjective(
        target_ah=target_ah,
        simulated_capacity_ah=simulated_capacity_ah,
        absolute_error_ah=absolute_error,
        relative_error=relative_error,
        passed=relative_error <= CAPACITY_RELATIVE_TOLERANCE,
    )


def soh_metrics(
    simulated_capacities: Mapping[int, float],
    experimental_capacities: Mapping[int, float],
    nodes: Sequence[int],
) -> SohMetrics:
    """Compute independently normalised SOH errors for exactly ``nodes``."""
    required = tuple(nodes)
    if len(required) < 2 or len(set(required)) != len(required) or required[0] != 0:
        raise ObjectiveError("SOH nodes must be unique, start with cycle 0, and include a scored node")
    if set(simulated_capacities) != set(required) or set(experimental_capacities) != set(required):
        raise ObjectiveError("SOH capacities must contain exactly the requested nodes")
    values = tuple(simulated_capacities.values()) + tuple(experimental_capacities.values())
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ObjectiveError("SOH capacities must be finite and positive")
    q_sim0, q_exp0 = simulated_capacities[0], experimental_capacities[0]
    result = tuple(
        SohNodeMetric(
            cycle=cycle,
            simulated_capacity_ah=simulated_capacities[cycle],
            experimental_capacity_ah=experimental_capacities[cycle],
            simulated_soh_pct=100.0 * simulated_capacities[cycle] / q_sim0,
            experimental_soh_pct=100.0 * experimental_capacities[cycle] / q_exp0,
            signed_error_pp=100.0 * simulated_capacities[cycle] / q_sim0 - 100.0 * experimental_capacities[cycle] / q_exp0,
            absolute_error_pp=abs(100.0 * simulated_capacities[cycle] / q_sim0 - 100.0 * experimental_capacities[cycle] / q_exp0),
        )
        for cycle in required
    )
    scored = result[1:]
    errors = np.asarray([item.signed_error_pp for item in scored], dtype=float)
    return SohMetrics(
        nodes=result,
        rmse_pp=float(np.sqrt(np.mean(errors**2))),
        max_absolute_error_pp=max(item.absolute_error_pp for item in scored),
        endpoint_absolute_error_pp=result[-1].absolute_error_pp,
    )


def assess_stage1(
    calibration: SohMetrics,
    holdout: SohMetrics | None = None,
) -> Stage1Acceptance:
    """Apply the three fixed thresholds, with boundary equality accepted."""
    if holdout is None:
        return Stage1Acceptance(calibration.rmse_pp <= CALIBRATION_RMSE_LIMIT_PP, None, None)
    cycle_350 = next((node for node in holdout.nodes if node.cycle == 350), None)
    if cycle_350 is None:
        raise ObjectiveError("holdout metrics must include cycle 350")
    return Stage1Acceptance(
        calibration.rmse_pp <= CALIBRATION_RMSE_LIMIT_PP,
        holdout.rmse_pp <= HOLDOUT_RMSE_LIMIT_PP,
        cycle_350.absolute_error_pp <= CYCLE_350_ABS_LIMIT_PP,
    )


def rank_candidates(candidates: Sequence[CandidateScore]) -> tuple[CandidateScore, ...]:
    """Return deterministic stage-1 calibration ranking without censored runs."""
    valid = [item for item in candidates if not item.numerically_censored and item.metrics is not None]
    valid.sort(key=lambda item: (item.metrics.rmse_pp, item.candidate_id))
    if not valid:
        return ()
    # RMSE values within 0.1 pp are a tie group; compare only its prescribed breakers.
    groups: list[list[CandidateScore]] = []
    for item in valid:
        if not groups or item.metrics.rmse_pp - groups[-1][0].metrics.rmse_pp > 0.1:
            groups.append([item])
        else:
            groups[-1].append(item)
    return tuple(
        candidate
        for group in groups
        for candidate in sorted(
            group,
            key=lambda item: (
                item.metrics.max_absolute_error_pp,
                item.metrics.endpoint_absolute_error_pp,
                sum(value * value for value in item.log10_scales),
                item.retry_count,
                item.candidate_id,
            ),
        )
    )


def _stable_capacity_curve(capacity: np.ndarray, voltage: np.ndarray, label: str) -> tuple[np.ndarray, np.ndarray]:
    if capacity.ndim != 1 or voltage.ndim != 1 or capacity.size != voltage.size or capacity.size < 2:
        raise ObjectiveError(f"{label} curve must contain matching one-dimensional arrays")
    if not np.all(np.isfinite(capacity)) or not np.all(np.isfinite(voltage)):
        raise ObjectiveError(f"{label} curve contains non-finite values")
    if np.any(np.diff(capacity) < 0):
        raise ObjectiveError(f"{label} capacity is non-monotonic")
    values, inverse = np.unique(capacity, return_inverse=True)
    summed = np.bincount(inverse, weights=voltage)
    counts = np.bincount(inverse)
    averaged_voltage = summed / counts
    if values.size < 2 or values[-1] <= values[0]:
        raise ObjectiveError(f"{label} curve has no positive capacity span")
    normalised = (values - values[0]) / (values[-1] - values[0])
    return normalised, averaged_voltage


def voltage_curve_metrics(
    simulated_capacity_ah: np.ndarray,
    simulated_voltage_v: np.ndarray,
    experimental_capacity_ah: np.ndarray,
    experimental_voltage_v: np.ndarray,
) -> VoltageCurveMetrics:
    """Interpolate two curves onto the fixed normalised-capacity grid."""
    sim_capacity = np.asarray(simulated_capacity_ah, dtype=float)
    sim_voltage = np.asarray(simulated_voltage_v, dtype=float)
    exp_capacity = np.asarray(experimental_capacity_ah, dtype=float)
    exp_voltage = np.asarray(experimental_voltage_v, dtype=float)
    sim_x, sim_y = _stable_capacity_curve(sim_capacity, sim_voltage, "simulated")
    exp_x, exp_y = _stable_capacity_curve(exp_capacity, exp_voltage, "experimental")
    grid = np.linspace(0.0, 1.0, VOLTAGE_GRID_POINTS)
    interpolated_simulated = np.interp(grid, sim_x, sim_y)
    interpolated_experimental = np.interp(grid, exp_x, exp_y)
    errors = interpolated_simulated - interpolated_experimental
    full_rmse = float(np.sqrt(np.mean(errors**2)))
    middle = (grid >= 0.10) & (grid <= 0.90)
    mid_rmse = float(np.sqrt(np.mean(errors[middle] ** 2)))
    endpoint_error = abs((sim_capacity[-1] - sim_capacity[0]) - (exp_capacity[-1] - exp_capacity[0])) / (
        exp_capacity[-1] - exp_capacity[0]
    )
    return VoltageCurveMetrics(
        full_rmse_v=full_rmse,
        mid_rmse_v=mid_rmse,
        max_abs_error_v=float(np.max(np.abs(errors))),
        endpoint_capacity_relative_error=float(endpoint_error),
        status=(
            "CAPACITY_MATCHED_VOLTAGE_PASSED"
            if full_rmse <= VOLTAGE_FULL_RMSE_LIMIT_V
            else "CAPACITY_MATCHED_VOLTAGE_FAILED"
        ),
        normalized_capacity_grid=grid,
        simulated_voltage_v=interpolated_simulated,
        experimental_voltage_v=interpolated_experimental,
    )
