"""Small explicit calibration state machine; it schedules no numerical work."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .artifacts import write_calibration_json


class CalibrationState(StrEnum):
    DATA_AUDITED = "DATA_AUDITED"
    CAPACITY_CALIBRATION_READY = "CAPACITY_CALIBRATION_READY"
    CAPACITY_CALIBRATED = "CAPACITY_CALIBRATED"
    AGING_DATA_INCOMPLETE = "AGING_DATA_INCOMPLETE"
    AGING_CALIBRATION_READY = "AGING_CALIBRATION_READY"
    SURROGATE_SCREENED = "SURROGATE_SCREENED"
    SPME_CALIBRATED = "SPME_CALIBRATED"
    PARAMETERS_FROZEN = "PARAMETERS_FROZEN"
    HOLDOUT_EVALUATED = "HOLDOUT_EVALUATED"


class CalibrationTransitionError(ValueError):
    pass


_TRANSITIONS = {
    CalibrationState.DATA_AUDITED: {CalibrationState.CAPACITY_CALIBRATION_READY},
    CalibrationState.CAPACITY_CALIBRATION_READY: {CalibrationState.CAPACITY_CALIBRATED},
    CalibrationState.CAPACITY_CALIBRATED: {
        CalibrationState.AGING_DATA_INCOMPLETE,
        CalibrationState.AGING_CALIBRATION_READY,
    },
    CalibrationState.AGING_CALIBRATION_READY: {CalibrationState.SURROGATE_SCREENED},
    CalibrationState.SURROGATE_SCREENED: {CalibrationState.SPME_CALIBRATED},
    CalibrationState.SPME_CALIBRATED: {CalibrationState.PARAMETERS_FROZEN},
    CalibrationState.PARAMETERS_FROZEN: {CalibrationState.HOLDOUT_EVALUATED},
}


@dataclass
class CalibrationWorkflow:
    output_dir: Path
    parameter_fingerprint: str
    state: CalibrationState = CalibrationState.DATA_AUDITED
    reason: str | None = None
    history: list[str] = field(default_factory=lambda: [CalibrationState.DATA_AUDITED.value])

    def __post_init__(self) -> None:
        if len(self.parameter_fingerprint) != 64:
            raise CalibrationTransitionError("parameter_fingerprint must be a SHA-256 digest")
        int(self.parameter_fingerprint, 16)
        self._write_status()

    def _write_status(self) -> None:
        write_calibration_json(
            self.output_dir / "calibration_status.json",
            {
                "state": self.state.value,
                "reason": self.reason,
                "parameter_fingerprint": self.parameter_fingerprint,
                "history": self.history,
                "holdout_accessed": False,
            },
        )

    def transition(self, target: CalibrationState, *, reason: str | None = None) -> None:
        if target not in _TRANSITIONS.get(self.state, set()):
            raise CalibrationTransitionError(f"invalid calibration transition: {self.state.value} -> {target.value}")
        self.state = target
        self.reason = reason
        self.history.append(target.value)
        self._write_status()

    def record_capacity_result(self, result: dict[str, Any]) -> None:
        if self.state is not CalibrationState.CAPACITY_CALIBRATION_READY:
            raise CalibrationTransitionError("capacity result requires CAPACITY_CALIBRATION_READY")
        write_calibration_json(self.output_dir / "capacity_calibration.json", result)
        self.transition(CalibrationState.CAPACITY_CALIBRATED)

    def apply_aging_data_gate(self, gate: dict[str, Any]) -> None:
        if self.state is not CalibrationState.CAPACITY_CALIBRATED:
            raise CalibrationTransitionError("aging data gate requires CAPACITY_CALIBRATED")
        status = gate.get("status")
        if status == CalibrationState.AGING_DATA_INCOMPLETE.value:
            self.transition(CalibrationState.AGING_DATA_INCOMPLETE, reason=str(gate.get("reason")))
        elif status == CalibrationState.AGING_CALIBRATION_READY.value:
            self.transition(CalibrationState.AGING_CALIBRATION_READY)
        else:
            raise CalibrationTransitionError(f"unknown aging data gate: {status!r}")
