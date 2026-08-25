from __future__ import annotations

from dataclasses import replace

import pytest

from pybamm_w10.calibration.parameters import (
    CalibrationParameterError,
    CalibrationParameters,
    require_formal_run_ready,
)
from pybamm_w10.config import RunConfig
from pybamm_w10.model import build_spme


def test_calibration_parameters_apply_only_the_allowed_keys() -> None:
    baseline = build_spme(RunConfig())
    calibrated = CalibrationParameters(
        calibration_id="m50t-w10-v1",
        calibration_status="CAPACITY_CALIBRATED",
        capacity_scale_factor=0.95,
        sei_scale=1.2,
        plating_scale=0.8,
        lam_scale=1.1,
    )
    artifacts = build_spme(RunConfig(), calibrated)

    assert artifacts.parameter_values["Electrode width [m]"] == pytest.approx(
        baseline.parameter_values["Electrode width [m]"] * 0.95
    )
    for key, factor in (
        ("SEI solvent diffusivity [m2.s-1]", 1.2),
        ("Lithium plating kinetic rate constant [m.s-1]", 0.8),
        ("Negative electrode LAM constant proportional term [s-1]", 1.1),
        ("Positive electrode LAM constant proportional term [s-1]", 1.1),
    ):
        assert artifacts.parameter_values[key] == pytest.approx(baseline.parameter_values[key] * factor)
    for key in (
        "Cell volume [m3]",
        "Cell cooling surface area [m2]",
        "Electrode height [m]",
        "Negative electrode thickness [m]",
        "Positive electrode thickness [m]",
        "Negative electrode active material volume fraction",
        "Positive electrode active material volume fraction",
    ):
        assert artifacts.parameter_values[key] == baseline.parameter_values[key]


def test_parameter_bounds_fingerprint_and_freeze_gate() -> None:
    parameters = CalibrationParameters(calibration_id="m50t-w10-v1")
    assert parameters.fingerprint == CalibrationParameters.from_json(parameters.to_json()).fingerprint
    with pytest.raises(CalibrationParameterError, match="capacity_scale_factor"):
        CalibrationParameters(capacity_scale_factor=1.03)
    with pytest.raises(CalibrationParameterError, match="not_calibrated"):
        require_formal_run_ready(replace(parameters, calibration_status="PARAMETERS_FROZEN", full_dfn_confirmed=True))
    ready = CalibrationParameters(
        calibration_status="PARAMETERS_FROZEN",
        full_dfn_confirmed=True,
        degradation_parameter_status="calibrated",
    )
    require_formal_run_ready(ready)
