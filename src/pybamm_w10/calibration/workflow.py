"""Small explicit calibration state machine; it schedules no numerical work."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
import json

from .artifacts import write_calibration_json


class CalibrationState(StrEnum):
    DATA_AUDITED = "DATA_AUDITED"
    CAPACITY_CALIBRATION_READY = "CAPACITY_CALIBRATION_READY"
    CAPACITY_CALIBRATED = "CAPACITY_CALIBRATED"
    AGING_DATA_INCOMPLETE = "AGING_DATA_INCOMPLETE"
    AGING_CALIBRATION_READY = "AGING_CALIBRATION_READY"
    PROBING = "PROBING"
    COMBINATIONS_PROPOSED = "COMBINATIONS_PROPOSED"
    SURROGATE_SCREENED = "SURROGATE_SCREENED"
    SPME_CALIBRATED = "SPME_CALIBRATED"
    PARAMETERS_FROZEN = "PARAMETERS_FROZEN"
    VALIDATING = "VALIDATING"
    HOLDOUT_EVALUATED = "HOLDOUT_EVALUATED"
    COMPLETED = "COMPLETED"
    CALIBRATION_FAILED = "CALIBRATION_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATION_NUMERICAL_FAILURE = "VALIDATION_NUMERICAL_FAILURE"


class CalibrationTransitionError(ValueError):
    pass


_TRANSITIONS = {
    CalibrationState.DATA_AUDITED: {CalibrationState.CAPACITY_CALIBRATION_READY},
    CalibrationState.CAPACITY_CALIBRATION_READY: {CalibrationState.CAPACITY_CALIBRATED},
    CalibrationState.CAPACITY_CALIBRATED: {
        CalibrationState.AGING_DATA_INCOMPLETE,
        CalibrationState.AGING_CALIBRATION_READY,
    },
    CalibrationState.AGING_CALIBRATION_READY: {CalibrationState.SURROGATE_SCREENED, CalibrationState.PROBING},
    CalibrationState.PROBING: {CalibrationState.COMBINATIONS_PROPOSED},
    CalibrationState.COMBINATIONS_PROPOSED: {CalibrationState.SPME_CALIBRATED},
    CalibrationState.SURROGATE_SCREENED: {CalibrationState.SPME_CALIBRATED},
    CalibrationState.SPME_CALIBRATED: {CalibrationState.PARAMETERS_FROZEN},
    CalibrationState.PARAMETERS_FROZEN: {CalibrationState.HOLDOUT_EVALUATED, CalibrationState.VALIDATING},
    CalibrationState.VALIDATING: {CalibrationState.HOLDOUT_EVALUATED},
    CalibrationState.HOLDOUT_EVALUATED: {CalibrationState.COMPLETED},
}
_TERMINAL_FAILURES = {
    CalibrationState.CALIBRATION_FAILED,
    CalibrationState.VALIDATION_FAILED,
    CalibrationState.VALIDATION_NUMERICAL_FAILURE,
}


@dataclass
class CalibrationWorkflow:
    output_dir: Path
    parameter_fingerprint: str
    state: CalibrationState = CalibrationState.DATA_AUDITED
    reason: str | None = None
    history: list[str] = field(default_factory=lambda: [CalibrationState.DATA_AUDITED.value])
    status_path: Path | None = None
    holdout_accessed: bool = False

    def __post_init__(self) -> None:
        if len(self.parameter_fingerprint) != 64:
            raise CalibrationTransitionError("parameter_fingerprint must be a SHA-256 digest")
        int(self.parameter_fingerprint, 16)
        self.status_path = self.status_path or self.output_dir / "calibration_status.json"
        self._write_status()

    @classmethod
    def resume(cls, output_dir: Path, *, status_path: Path, parameter_fingerprint: str) -> "CalibrationWorkflow":
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            state = CalibrationState(value["state"])
            history = list(value["history"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise CalibrationTransitionError("cannot resume calibration status") from exc
        if value.get("parameter_fingerprint") != parameter_fingerprint:
            raise CalibrationTransitionError("calibration status parameter fingerprint does not match")
        return cls(
            output_dir,
            parameter_fingerprint,
            state,
            value.get("reason"),
            history,
            status_path,
            bool(value.get("holdout_accessed", False)),
        )

    def _write_status(self) -> None:
        write_calibration_json(
            self.status_path,
            {
                "state": self.state.value,
                "reason": self.reason,
                "parameter_fingerprint": self.parameter_fingerprint,
                "history": self.history,
                "holdout_accessed": self.holdout_accessed,
            },
        )

    def transition(self, target: CalibrationState, *, reason: str | None = None) -> None:
        if target not in _TERMINAL_FAILURES and target not in _TRANSITIONS.get(self.state, set()):
            raise CalibrationTransitionError(f"invalid calibration transition: {self.state.value} -> {target.value}")
        self.state = target
        if target is CalibrationState.HOLDOUT_EVALUATED:
            self.holdout_accessed = True
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
