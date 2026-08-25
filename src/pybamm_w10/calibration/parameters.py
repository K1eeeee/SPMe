"""Versioned, bounded calibration parameters and their PyBaMM mapping."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any


PARAMETER_SCHEMA_VERSION = 1
CAPACITY_SCALE_BOUNDS = (0.90, 1.02)
DEGRADATION_LOG10_BOUNDS = (-1.0, 1.0)
PYBAMM_PARAMETER_KEYS = {
    "capacity_scale_factor": ("Electrode width [m]",),
    "sei_scale": ("SEI solvent diffusivity [m2.s-1]",),
    "plating_scale": ("Lithium plating kinetic rate constant [m.s-1]",),
    "lam_scale": (
        "Negative electrode LAM constant proportional term [s-1]",
        "Positive electrode LAM constant proportional term [s-1]",
    ),
}


class CalibrationParameterError(ValueError):
    """A parameter artifact is invalid or not approved for its requested use."""


@dataclass(frozen=True)
class CalibrationParameters:
    calibration_id: str = "OKane2022-M50T-W10-v1"
    calibration_status: str = "BASELINE"
    capacity_scale_factor: float = 1.0
    sei_scale: float = 1.0
    plating_scale: float = 1.0
    lam_scale: float = 1.0
    degradation_parameter_status: str = "not_calibrated"
    model: str = "SPMe"
    source_model: str = "SPMe"
    mode: str = "strict-w10"
    full_dfn_confirmed: bool = False
    holdout_accessed: bool = False
    schema_version: int = PARAMETER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PARAMETER_SCHEMA_VERSION:
            raise CalibrationParameterError(f"unsupported parameter schema: {self.schema_version}")
        if not self.calibration_id:
            raise CalibrationParameterError("calibration_id must be non-empty")
        if self.model != "SPMe" or self.mode != "strict-w10":
            raise CalibrationParameterError("calibration parameters must target strict-w10 SPMe")
        if self.source_model not in {"DFN", "SPMe"}:
            raise CalibrationParameterError("calibration source_model must be DFN or SPMe")
        if not all(math.isfinite(value) for value in self.values.values()):
            raise CalibrationParameterError("calibration parameters must be finite")
        low, high = CAPACITY_SCALE_BOUNDS
        if not low <= self.capacity_scale_factor <= high:
            raise CalibrationParameterError(f"capacity_scale_factor must be within [{low}, {high}]")
        log_low, log_high = DEGRADATION_LOG10_BOUNDS
        for name in ("sei_scale", "plating_scale", "lam_scale"):
            value = self.values[name]
            if value <= 0 or not log_low <= math.log10(value) <= log_high:
                raise CalibrationParameterError(f"{name} log10 value must be within [{log_low}, {log_high}]")
        if self.calibration_status == "PARAMETERS_FROZEN" and self.degradation_parameter_status == "not_calibrated":
            raise CalibrationParameterError("not_calibrated degradation parameters cannot be PARAMETERS_FROZEN")

    @property
    def values(self) -> dict[str, float]:
        return {
            "capacity_scale_factor": self.capacity_scale_factor,
            "sei_scale": self.sei_scale,
            "plating_scale": self.plating_scale,
            "lam_scale": self.lam_scale,
        }

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "calibration_id": self.calibration_id,
            "calibration_status": self.calibration_status,
            "parameters": self.values,
            "parameter_keys": {name: list(keys) for name, keys in PYBAMM_PARAMETER_KEYS.items()},
            "degradation_parameter_status": self.degradation_parameter_status,
            "model": self.model,
            "source_model": self.source_model,
            "mode": self.mode,
            "full_dfn_confirmed": self.full_dfn_confirmed,
            "holdout_accessed": self.holdout_accessed,
        }

    @property
    def fingerprint(self) -> str:
        return sha256(
            json.dumps(self._payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def to_json(self) -> dict[str, object]:
        return {**self._payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "CalibrationParameters":
        try:
            parameters = value["parameters"]
            result = cls(
                calibration_id=str(value["calibration_id"]),
                calibration_status=str(value["calibration_status"]),
                capacity_scale_factor=float(parameters["capacity_scale_factor"]),
                sei_scale=float(parameters["sei_scale"]),
                plating_scale=float(parameters["plating_scale"]),
                lam_scale=float(parameters["lam_scale"]),
                degradation_parameter_status=str(value["degradation_parameter_status"]),
                model=str(value["model"]),
                source_model=str(value.get("source_model", value["model"])),
                mode=str(value["mode"]),
                full_dfn_confirmed=bool(value["full_dfn_confirmed"]),
                holdout_accessed=bool(value["holdout_accessed"]),
                schema_version=int(value["schema_version"]),
            )
            supplied_fingerprint = str(value["fingerprint"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CalibrationParameterError("invalid calibration parameter artifact") from exc
        if supplied_fingerprint != result.fingerprint:
            raise CalibrationParameterError("calibration parameter fingerprint does not match content")
        expected_mapping = {name: list(keys) for name, keys in PYBAMM_PARAMETER_KEYS.items()}
        if value.get("parameter_keys") != expected_mapping:
            raise CalibrationParameterError("calibration parameter PyBaMM key mapping does not match schema")
        return result


def load_calibration_parameters(path: Path) -> CalibrationParameters:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationParameterError(f"cannot read calibration parameters: {path}") from exc
    if not isinstance(value, dict):
        raise CalibrationParameterError("calibration parameter artifact must be a JSON object")
    return CalibrationParameters.from_json(value)


def apply_calibration_parameters(parameter_values: Any, parameters: CalibrationParameters) -> None:
    """Apply only the approved scalar mappings to an already-overridden set."""
    updates: dict[str, float] = {}
    for name, keys in PYBAMM_PARAMETER_KEYS.items():
        factor = parameters.values[name]
        for key in keys:
            updates[key] = float(parameter_values[key]) * factor
    parameter_values.update(updates)


def require_formal_run_ready(parameters: CalibrationParameters) -> None:
    if parameters.calibration_status != "PARAMETERS_FROZEN":
        raise CalibrationParameterError("formal run requires PARAMETERS_FROZEN calibration parameters")
    if not parameters.full_dfn_confirmed:
        raise CalibrationParameterError("formal run requires full_dfn_confirmed=true")
    if parameters.degradation_parameter_status == "not_calibrated":
        raise CalibrationParameterError("formal run rejects not_calibrated degradation parameters")
