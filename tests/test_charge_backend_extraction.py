from __future__ import annotations

import numpy as np
from types import SimpleNamespace

from pybamm_w10.backend import PyBaMMBackend
from pybamm_w10.charge_variables import preflight_charge_variables


class _Variable:
    def __init__(self, time, values) -> None:
        self.time = np.asarray(time, dtype=float)
        self.values = np.asarray(values, dtype=float)

    def __call__(self, time):
        return np.interp(np.asarray(time, dtype=float), self.time, self.values)


class _Solution:
    def __init__(self, variables) -> None:
        self.t = np.array([0.0, 1.0, 2.0])
        self.variables = variables

    def __getitem__(self, name):
        return self.variables[name]


class _Model:
    name = "fake-spme"

    def __init__(self, variables) -> None:
        self.variables = variables


def test_backend_extracts_only_requested_charge_stage_without_solving() -> None:
    from pybamm_w10.charge_variables import CHARGE_VARIABLE_ROLES

    variables = {role.candidate_names[0]: _Variable((0.0, 1.0, 2.0), (1.0, 2.0, 3.0)) for role in CHARGE_VARIABLE_ROLES}
    resolved = preflight_charge_variables(_Model(variables), model_options={})
    backend = object.__new__(PyBaMMBackend)
    backend.solution = _Solution(variables)
    backend.solution.t = np.array([0.0, 1.0])
    backend._committed_segments = [SimpleNamespace(
        global_start_s=250_000.0,
        global_end_s=250_001.0,
        local_solution=backend.solution,
    )]
    backend.artifacts = SimpleNamespace(parameter_values={
        "Negative electrode thickness [m]": 1.0,
        "Electrode width [m]": 1.0,
        "Electrode height [m]": 1.0,
    })

    trace = backend.extract_charge_stage_trace("3c_cc", 250_000.0, 250_001.0, resolved)

    assert trace.stage_name == "3c_cc"
    assert trace.time_s == (0.0, 1.0)
    assert trace.global_time_offset_s == 250_000.0
    assert trace.values["current_a"] == (1.0, 2.0)


def test_charge_series_takes_spatial_minimum_at_each_time() -> None:
    raw = np.array(((0.20, 0.10, 0.05), (0.15, 0.12, 0.08)))

    result = PyBaMMBackend._charge_series(raw, 3, "spatiotemporal_min")

    assert result == (0.15, 0.10, 0.05)
