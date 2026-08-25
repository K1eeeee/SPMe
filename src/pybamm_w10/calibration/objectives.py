"""Deterministic cycle-0 capacity and voltage objectives without any solver calls."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


CAPACITY_TARGET_AH = 4.865884391243259
CAPACITY_RELATIVE_TOLERANCE = 0.002
VOLTAGE_GRID_POINTS = 1001
VOLTAGE_FULL_RMSE_LIMIT_V = 0.050


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
