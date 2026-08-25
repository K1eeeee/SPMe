from __future__ import annotations

import csv
from types import SimpleNamespace

import numpy as np

from pybamm_w10.backend import PyBaMMBackend
from pybamm_w10.output import append_dataclass, append_degradation_summary
from pybamm_w10.types import CycleResult


class _Variable:
    def __init__(self, values) -> None:
        self.values = np.asarray(values, dtype=float)

    def __call__(self, time):
        requested = np.asarray(time, dtype=float)
        return np.interp(requested, (0.0, 1.0), self.values)


class _Solution:
    def __init__(self, variables) -> None:
        self.t = np.array((0.0, 1.0))
        self.variables = variables

    def __getitem__(self, name):
        return self.variables[name]


def _backend() -> PyBaMMBackend:
    variables = {
        "Terminal voltage [V]": _Variable((3.5, 3.6)),
        "Current [A]": _Variable((-1.0, -1.0)),
        "Discharge capacity [A.h]": _Variable((0.0, 0.0)),
        "X-averaged cell temperature [K]": _Variable((296.15, 297.15)),
        "Ambient temperature [K]": _Variable((296.15, 296.15)),
        "Loss of lithium inventory [%]": _Variable((0.0, 1.25)),
        "Loss of active material in negative electrode [%]": _Variable((0.0, 0.2)),
        "Loss of active material in positive electrode [%]": _Variable((0.0, 0.1)),
        "X-averaged negative electrode porosity": _Variable((0.25, 0.24)),
        "X-averaged positive electrode porosity": _Variable((0.335, 0.335)),
        "X-averaged negative electrode active material volume fraction": _Variable((0.75, 0.74)),
        "X-averaged positive electrode active material volume fraction": _Variable((0.665, 0.66)),
        "X-averaged negative SEI thickness [m]": _Variable((5e-9, 6e-9)),
        "X-averaged negative SEI on cracks thickness [m]": _Variable((0.0, 2e-9)),
        "Loss of capacity to negative SEI [A.h]": _Variable((0.0, 0.01)),
        "Loss of capacity to negative SEI on cracks [A.h]": _Variable((0.0, 0.02)),
        "Loss of capacity to negative lithium plating [A.h]": _Variable((0.0, 0.05)),
        "Volume-averaged negative dead lithium concentration [mol.m-3]": _Variable((0.0, 2.0)),
    }
    backend = object.__new__(PyBaMMBackend)
    backend.solution = _Solution(variables)
    backend.artifacts = SimpleNamespace(parameter_values={
        "Negative electrode thickness [m]": 1e-4,
        "Electrode width [m]": 1.0,
        "Electrode height [m]": 1.0,
    })
    return backend


def _cycle(metrics: dict[str, float]) -> CycleResult:
    return CycleResult(
        cycle=1, mode="virtual", q_ref_ah=4.0, q_ref_node=0,
        step5_target_ah=0.8, window_target_ah=3.2, delta_q5_actual_ah=0.8,
        actual_udds_remaining_target_ah=2.4, udds_profile_available_ah=2.412,
        udds_guard_ah=0.012, udds_actual_ah=2.4, window_actual_ah=3.2,
        start_time_s=0.0, end_time_s=1.0, metrics=metrics,
    )


def test_aging_metrics_are_finite_and_inventory_identities_hold() -> None:
    metrics = _backend().summary_metrics(0.0)
    requested = {
        "lli_pct", "normal_sei_loss_ah", "sei_on_cracks_loss_ah",
        "total_sei_loss_ah", "total_plated_lithium_ah", "dead_lithium_ah",
        "reversible_plated_lithium_ah", "negative_sei_thickness_m",
        "negative_sei_on_cracks_thickness_m", "temperature_max_k",
        "ambient_temperature_k", "temperature_rise_max_k",
    }

    assert requested <= metrics.keys()
    assert all(np.isfinite(metrics[key]) for key in requested)
    assert metrics["total_sei_loss_ah"] == 0.03
    assert metrics["sei_loss_ah"] == metrics["total_sei_loss_ah"]
    assert metrics["reversible_plated_lithium_ah"] == (
        metrics["total_plated_lithium_ah"] - metrics["dead_lithium_ah"]
    )
    assert metrics["temperature_rise_max_k"] == 1.0


def test_new_metrics_are_nonempty_in_both_cycle_csv_outputs(workspace_tmp) -> None:
    metrics = _backend().summary_metrics(0.0)
    metrics["negative_electrode_min_potential_v"] = 0.04
    cycle = _cycle(metrics)
    cycle_path = workspace_tmp / "cycle_summary.csv"
    degradation_path = workspace_tmp / "degradation_summary.csv"

    append_dataclass(cycle_path, cycle)
    append_degradation_summary(degradation_path, cycle)

    requested = {
        "lli_pct", "normal_sei_loss_ah", "sei_on_cracks_loss_ah",
        "total_sei_loss_ah", "total_plated_lithium_ah", "dead_lithium_ah",
        "reversible_plated_lithium_ah", "negative_sei_thickness_m",
        "negative_sei_on_cracks_thickness_m", "negative_electrode_min_potential_v",
        "temperature_max_k", "ambient_temperature_k", "temperature_rise_max_k",
    }
    for path in (cycle_path, degradation_path):
        with path.open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert requested <= row.keys()
        assert all(row[key] != "" and np.isfinite(float(row[key])) for key in requested)
