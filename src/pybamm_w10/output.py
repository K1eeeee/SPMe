"""Atomic outputs, checkpoint commit manifests, rollback, and run locking."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import csv
import io
from hashlib import sha256
import json
import os
from pathlib import Path
import pickle
import shutil
import socket
import tempfile
from typing import Any, BinaryIO

from .config import RunConfig
from .types import (
    AppendFileCommit,
    ArtifactCommit,
    Checkpoint,
    ChargeEfficiencySummary,
    ChargeSocBinResult,
    ChargeTraceArtifact,
    CycleResult,
    FailureContext,
    NumericalFailure,
    OutputCommitManifest,
    RPTResult,
    RunStatus,
)
from .udds import CurrentProfile


APPEND_OUTPUTS = (
    "cycle_summary.csv",
    "rpt_summary.csv",
    "degradation_summary.csv",
    "charge_efficiency_summary.csv",
    "charge_efficiency_soc_bins.csv",
    "udds_cycle_validation.jsonl",
    "solver_attempts.jsonl",
    "run.log",
)

CYCLE_SUMMARY_V3_FIELDS = (
    "cycle", "mode", "q_ref_ah", "q_ref_node", "step5_target_ah", "window_target_ah",
    "delta_q5_actual_ah", "actual_udds_remaining_target_ah", "udds_profile_available_ah",
    "udds_guard_ah", "udds_actual_ah", "window_actual_ah", "start_time_s", "end_time_s",
    "termination_event", "termination_time_s", "termination_value", "termination_classification",
    "configured_nominal_charge_rate_c", "effective_charge_rate_c", "useful_charge_efficiency_pct",
    "reversible_retention_pct", "charge_efficiency_status", "complete_soc_bin_count",
    "output_schema_version", "duration_3c_cc_s", "duration_4v_cv_s", "duration_c4_cc_s",
    "duration_4p2v_cv_s", "duration_post_charge_rest_s", "duration_rpt_recovery_cc_s",
    "duration_rpt_recovery_cv_s", "duration_rpt_recovery_rest_s", "duration_step5_c4_discharge_s",
    "duration_step6_udds_s", "wall_clock_3c_cc_s", "wall_clock_4v_cv_s", "wall_clock_c4_cc_s",
    "wall_clock_4p2v_cv_s", "wall_clock_post_charge_rest_s", "wall_clock_rpt_recovery_cc_s",
    "wall_clock_rpt_recovery_cv_s", "wall_clock_rpt_recovery_rest_s", "wall_clock_step5_c4_discharge_s",
    "wall_clock_step6_udds_s", "terminal_voltage_v", "temperature_k", "temperature_min_k",
    "temperature_max_k", "ambient_temperature_k", "temperature_rise_max_k",
    "charge_ah", "discharge_ah", "net_ah", "lli_pct", "normal_sei_loss_ah",
    "sei_on_cracks_loss_ah", "total_sei_loss_ah", "sei_loss_ah", "total_plated_lithium_ah",
    "dead_lithium_ah", "reversible_plated_lithium_ah", "plating_loss_ah",
    "dead_lithium_loss_ah", "negative_sei_thickness_m",
    "negative_sei_on_cracks_thickness_m", "negative_electrode_min_potential_v",
    "negative_lam_pct", "positive_lam_pct", "negative_porosity",
    "positive_porosity", "negative_active_material_fraction", "positive_active_material_fraction",
    "step5_relative_error", "udds_profile_net_discharge_ah", "udds_profile_target_error",
)

CHARGE_EFFICIENCY_SUMMARY_V3_FIELDS = (
    "cycle", "mode", "configured_charge_current_a", "configured_nominal_charge_rate_c",
    "effective_charge_rate_c", "nominal_capacity_ah", "q_ref_ah", "q_ref_node", "soh_pct",
    "soc_start_pct", "soc_at_charge_end_pct", "soc_definition", "soc_reference_capacity_ah",
    "capacity_reference_node", "soc_anchor_pct", "soc_anchor_source", "soc_anchor_validation_status",
    "time_start_s", "time_end_s", "duration_s", "post_100_charge_ah", "post_100_duration_s",
    "external_charge_ah", "cc_charge_ah", "cv_charge_ah", "cv_charge_fraction_pct",
    "negative_particle_lithium_mol_start", "negative_particle_lithium_mol_end",
    "faraday_constant_c_per_mol", "intercalated_charge_increment_ah",
    "total_plating_inventory_start_ah", "total_plating_inventory_end_ah",
    "reversible_plating_inventory_start_ah", "reversible_plating_inventory_end_ah",
    "reversible_plating_increment_ah", "reversible_plating_depletion_ah",
    "dead_lithium_inventory_start_ah", "dead_lithium_inventory_end_ah", "dead_lithium_increment_ah",
    "sei_inventory_start_ah", "sei_inventory_end_ah", "sei_increment_ah",
    "useful_charge_efficiency_pct", "reversible_retention_pct", "accounted_charge_ah",
    "charge_balance_error_ah", "charge_balance_error_pct", "charge_balance_abs_error_pct",
    "charge_balance_status", "reversible_plating_inventory_crosscheck_ah",
    "reversible_plating_crosscheck_error_ah", "reversible_plating_crosscheck_status",
    "charge_trace_path", "charge_trace_sha256", "charge_trace_row_count", "charge_integration_method",
    "charge_integration_point_count", "primary_status", "status_flags",
    "negative_electrode_min_potential_v",
    "is_valid_for_efficiency_analysis", "is_valid_for_mechanism_analysis",
    "output_schema_version", "charge_efficiency_algorithm_version",
)

CHARGE_EFFICIENCY_SOC_BIN_V3_FIELDS = (
    "cycle", "mode", "soc_bin_id", "soc_start_pct", "soc_end_pct", "actual_soc_start_pct",
    "actual_soc_end_pct", "soc_coverage_pct", "soc_definition", "soc_reference_capacity_ah",
    "capacity_reference_node", "soc_anchor_pct", "soc_anchor_source", "configured_charge_current_a",
    "configured_nominal_charge_rate_c", "effective_charge_rate_c", "nominal_capacity_ah", "soh_pct",
    "q_ref_ah", "q_ref_node", "lli_pct", "negative_lam_pct", "positive_lam_pct", "external_charge_ah",
    "negative_particle_lithium_mol_start", "negative_particle_lithium_mol_end",
    "intercalated_charge_increment_ah", "total_plating_inventory_start_ah",
    "total_plating_inventory_end_ah", "reversible_plating_inventory_start_ah",
    "reversible_plating_inventory_end_ah", "reversible_plating_increment_ah",
    "reversible_plating_depletion_ah", "dead_lithium_inventory_start_ah",
    "dead_lithium_inventory_end_ah", "dead_lithium_increment_ah", "sei_inventory_start_ah",
    "sei_inventory_end_ah", "sei_increment_ah", "useful_charge_efficiency_pct",
    "reversible_retention_pct", "charge_balance_error_ah", "charge_balance_error_pct",
    "charge_balance_abs_error_pct", "charge_balance_status", "time_start_s", "time_end_s", "duration_s",
    "cc_charge_ah", "cv_charge_ah", "cv_charge_fraction_pct", "mean_current_a", "mean_voltage_v",
    "maximum_voltage_v", "temperature_start_k", "temperature_end_k", "temperature_mean_k",
    "temperature_max_k", "temperature_rise_k", "negative_surface_stoichiometry",
    "negative_average_stoichiometry", "negative_particle_radial_stoichiometry_gradient",
    "minimum_electrolyte_concentration_mol_m3", "negative_reaction_overpotential_v",
    "plating_reaction_overpotential_v", "negative_intercalation_current_density_mean_a_m2",
    "negative_intercalation_current_density_max_a_m2", "negative_plating_current_density_mean_a_m2",
    "negative_plating_current_density_extreme_a_m2", "negative_sei_current_density_mean_a_m2",
    "negative_sei_current_density_max_a_m2", "electrolyte_ohmic_loss_mean_v",
    "electrolyte_ohmic_loss_max_v", "negative_particle_concentration_overpotential_mean_v",
    "negative_particle_concentration_overpotential_max_v", "irreversible_heating_energy_wh",
    "ohmic_heating_energy_wh", "reversible_heating_energy_wh", "total_heating_energy_wh",
    "soc_crossing_count", "soc_crossing_selection_rule", "charge_trace_path", "trace_start_row",
    "trace_end_row", "trace_start_time_s", "trace_end_time_s", "primary_status", "status_flags",
    "is_valid_for_efficiency_analysis", "is_valid_for_mechanism_analysis", "output_schema_version",
    "charge_efficiency_algorithm_version",
)

CHARGE_TIMESERIES_V3_FIELDS = (
    "cycle", "charge_stage", "time_s", "current_a", "terminal_voltage_v", "temperature_k",
    "reference_soc_pct", "cumulative_external_charge_ah", "negative_particle_lithium_mol",
    "total_plating_inventory_ah", "dead_lithium_inventory_ah", "reversible_plating_inventory_ah",
    "cumulative_sei_loss_ah", "soc_boundary",
)
STATIC_ARTIFACTS = (
    "run_config.json",
    "environment.json",
    "initial_state.json",
    "udds_profile.csv",
    "udds_validation.json",
    "run_status.json",
)


class RunDirectoryBusy(RuntimeError):
    pass


class RunDirectoryLock:
    """An OS-level exclusive lock held for the complete run/resume lifecycle."""

    def __init__(self, run_dir: Path, metadata: dict[str, Any]):
        self.run_dir = run_dir
        self.metadata = metadata
        self.path = run_dir / ".run.lock"
        self._handle: BinaryIO | None = None
        self.stale_metadata: dict[str, Any] | None = None
        self.business_status: str = "RUNNING"

    def __enter__(self) -> "RunDirectoryLock":
        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RunDirectoryBusy(f"run directory is already locked: {self.run_dir}") from exc
        self._handle = handle
        handle.seek(0)
        old = handle.read().lstrip(b"\0").strip()
        if old:
            try:
                self.stale_metadata = json.loads(old.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.stale_metadata = {"unparseable_lock_metadata_sha256": sha256(old).hexdigest()}
        active = {
            **self.metadata,
            "run_directory": str(self.run_dir.resolve()),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at_utc": datetime.now(timezone.utc).isoformat(),
            "released_at_utc": None,
            "business_status": self.business_status,
        }
        self._write_metadata(active)
        return self

    def set_business_status(self, status: RunStatus | str) -> None:
        """Record the run's business terminal state before releasing the lock."""
        self.business_status = status.value if isinstance(status, RunStatus) else str(status)
        if self._handle is None:
            return
        self._handle.seek(0)
        current = json.loads(self._handle.read().decode("utf-8"))
        current["business_status"] = self.business_status
        self._write_metadata(current)

    def _write_metadata(self, value: dict[str, Any]) -> None:
        assert self._handle is not None
        payload = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(payload)
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            current = json.loads(self._handle.read().decode("utf-8"))
            current["released_at_utc"] = datetime.now(timezone.utc).isoformat()
            current["release_reason"] = "normal" if exc_type is None else f"exception:{exc_type.__name__}"
            current["business_status"] = self.business_status
            self._write_metadata(current)
        finally:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def prepare_run_directory(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for child in ("checkpoints", "timeseries", "charge_timeseries", "figures", "rollback", "failures"):
        (root / child).mkdir(exist_ok=True)
    return root


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_json(path: Path, value: Any) -> None:
    _atomic_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"),
    )


def append_json_line(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip("\n") + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_profile(path: Path, profile: CurrentProfile) -> None:
    rows = ["time_s,current_a\n"]
    rows.extend(f"{time},{current}\n" for time, current in zip(profile.time_s, profile.current_a, strict=True))
    _atomic_bytes(path, "".join(rows).encode("utf-8"))


def _flat_dataclass(value: CycleResult | RPTResult) -> dict[str, Any]:
    row = asdict(value)
    row["output_schema_version"] = 3
    row.pop("charge_analysis", None)
    row.pop("timeseries", None)
    targets = row.pop("targets", None)
    if isinstance(value, RPTResult):
        row.update(
            {
                "next_step5_target_ah": value.next_step5_target_ah or "",
                "next_window_target_ah": value.next_window_target_ah or "",
                "planned_udds_remaining_ah": value.planned_udds_remaining_ah or "",
            }
        )
    durations = row.pop("stage_durations_s", None)
    wall_durations = row.pop("stage_wall_clock_durations_s", None)
    if isinstance(value, CycleResult):
        duration_keys = (
            "3c_cc",
            "4v_cv",
            "c4_cc",
            "4p2v_cv",
            "post_charge_rest",
            "rpt_recovery_cc",
            "rpt_recovery_cv",
            "rpt_recovery_rest",
            "step5_c4_discharge",
            "step6_udds",
        )
        row.update(
            {f"duration_{key}_s": (durations or {}).get(key, 0.0) for key in duration_keys}
        )
        row.update(
            {f"wall_clock_{key}_s": (wall_durations or {}).get(key, 0.0) for key in duration_keys}
        )
    metrics = row.pop("metrics", None)
    if isinstance(value, CycleResult):
        metric_keys = (
            "terminal_voltage_v",
            "temperature_k",
            "temperature_min_k",
            "temperature_max_k",
            "ambient_temperature_k",
            "temperature_rise_max_k",
            "charge_ah",
            "discharge_ah",
            "net_ah",
            "lli_pct",
            "normal_sei_loss_ah",
            "sei_on_cracks_loss_ah",
            "total_sei_loss_ah",
            "sei_loss_ah",
            "total_plated_lithium_ah",
            "dead_lithium_ah",
            "reversible_plated_lithium_ah",
            "plating_loss_ah",
            "dead_lithium_loss_ah",
            "negative_sei_thickness_m",
            "negative_sei_on_cracks_thickness_m",
            "negative_electrode_min_potential_v",
            "negative_lam_pct",
            "positive_lam_pct",
            "negative_porosity",
            "positive_porosity",
            "negative_active_material_fraction",
            "positive_active_material_fraction",
            "step5_relative_error",
            "udds_profile_net_discharge_ah",
            "udds_profile_target_error",
        )
        row.update({key: (metrics or {}).get(key, "") for key in metric_keys})
    return row


def _append_csv_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    fieldnames = list(row)
    if exists:
        with path.open(encoding="utf-8", newline="") as existing:
            fieldnames = next(csv.reader(existing))
        extra = set(row) - set(fieldnames)
        if extra:
            raise NumericalFailure(f"CSV schema changed for {path.name}: {sorted(extra)}")
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def append_dataclass(path: Path, value: CycleResult | RPTResult) -> None:
    row = _flat_dataclass(value)
    if isinstance(value, CycleResult):
        _append_csv_row(path, {field: row.get(field, "") for field in CYCLE_SUMMARY_V3_FIELDS})
    else:
        _append_csv_row(path, row)


def _clean_schema_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(str(item.value if hasattr(item, "value") else item) for item in value)
    return value.value if hasattr(value, "value") else value


def _specialized_row(fields: tuple[str, ...], *, cycle: int, mode: str, values: dict[str, Any]) -> dict[str, Any]:
    extras = set(values) - set(fields)
    if extras:
        raise NumericalFailure(f"unexpected specialized CSV fields: {sorted(extras)}")
    row = {field: "" for field in fields}
    row.update({field: _clean_schema_value(value) for field, value in values.items()})
    row["cycle"] = cycle
    row["mode"] = mode
    row["output_schema_version"] = 3
    row["charge_efficiency_algorithm_version"] = row.get("charge_efficiency_algorithm_version") or "charge-efficiency-v1"
    return row


def _append_csv_rows_atomic(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with path.open(encoding="utf-8", newline="") as existing:
            header = tuple(next(csv.reader(existing)))
        if header != fields:
            raise NumericalFailure(f"CSV schema changed for {path.name}")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    if not exists:
        writer.writeheader()
    writer.writerows(rows)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(buffer.getvalue())
        handle.flush()
        os.fsync(handle.fileno())


def append_charge_efficiency_summary(path: Path, summary: ChargeEfficiencySummary) -> None:
    row = _specialized_row(
        CHARGE_EFFICIENCY_SUMMARY_V3_FIELDS,
        cycle=summary.cycle,
        mode=summary.mode,
        values=dict(summary.values),
    )
    _append_csv_rows_atomic(path, CHARGE_EFFICIENCY_SUMMARY_V3_FIELDS, [row])


def append_charge_soc_bins(path: Path, rows: tuple[ChargeSocBinResult, ...]) -> None:
    expected = ("20-40", "40-60", "60-80", "80-100")
    if len(rows) != 4 or tuple(row.soc_bin_id for row in rows) != expected:
        raise NumericalFailure("charge SOC bins must be exactly the ordered 20-40/40-60/60-80/80-100 group")
    consistent = {(row.cycle, row.mode, row.values.get("q_ref_ah"), row.values.get("charge_efficiency_algorithm_version", "charge-efficiency-v1")) for row in rows}
    if len(consistent) != 1:
        raise NumericalFailure("charge SOC bin group has inconsistent cycle, mode, q_ref, or algorithm")
    serialized = []
    for item in rows:
        values = {**item.values, "soc_bin_id": item.soc_bin_id}
        serialized.append(_specialized_row(CHARGE_EFFICIENCY_SOC_BIN_V3_FIELDS, cycle=item.cycle, mode=item.mode, values=values))
    _append_csv_rows_atomic(path, CHARGE_EFFICIENCY_SOC_BIN_V3_FIELDS, serialized)


def write_charge_timeseries(path: Path, rows: tuple[dict[str, Any], ...]) -> ChargeTraceArtifact:
    if len(rows) < 2:
        raise NumericalFailure("charge trace requires at least two rows")
    normalized = []
    previous_time: float | None = None
    for row in rows:
        if set(row) - set(CHARGE_TIMESERIES_V3_FIELDS):
            raise NumericalFailure("charge trace contains an unknown field")
        values = {field: _clean_schema_value(row.get(field)) for field in CHARGE_TIMESERIES_V3_FIELDS}
        try:
            time = float(values["time_s"])
            float(values["current_a"])
        except (TypeError, ValueError) as exc:
            raise NumericalFailure("charge trace requires finite time_s and current_a") from exc
        if previous_time is not None and time <= previous_time:
            raise NumericalFailure("charge trace time must be strictly increasing")
        previous_time = time
        normalized.append(values)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CHARGE_TIMESERIES_V3_FIELDS)
    writer.writeheader()
    writer.writerows(normalized)
    _atomic_bytes(path, buffer.getvalue().encode("utf-8"))
    return ChargeTraceArtifact(
        path.as_posix(), _file_hash(path), len(normalized), float(normalized[0]["time_s"]), float(normalized[-1]["time_s"])
    )


def append_degradation_summary(path: Path, cycle: CycleResult) -> None:
    keys = (
        "lli_pct",
        "normal_sei_loss_ah",
        "sei_on_cracks_loss_ah",
        "total_sei_loss_ah",
        "sei_loss_ah",
        "total_plated_lithium_ah",
        "dead_lithium_ah",
        "reversible_plated_lithium_ah",
        "plating_loss_ah",
        "dead_lithium_loss_ah",
        "negative_sei_thickness_m",
        "negative_sei_on_cracks_thickness_m",
        "negative_electrode_min_potential_v",
        "temperature_max_k",
        "ambient_temperature_k",
        "temperature_rise_max_k",
        "negative_lam_pct",
        "positive_lam_pct",
        "negative_porosity",
        "positive_porosity",
        "negative_active_material_fraction",
        "positive_active_material_fraction",
    )
    row = {"cycle": cycle.cycle, "mode": cycle.mode}
    row.update({key: cycle.metrics.get(key, "") for key in keys})
    _append_csv_row(path, row)


def write_timeseries_csv(path: Path, values: dict[str, object]) -> None:
    if not values:
        raise NumericalFailure(f"cannot write an empty time series: {path}")
    keys = list(values)
    columns = [list(values[key]) for key in keys]
    lines = [",".join(keys) + "\n"]
    lines.extend(",".join(str(value) for value in row) + "\n" for row in zip(*columns, strict=True))
    _atomic_bytes(path, "".join(lines).encode("utf-8"))


def _file_hash(path: Path, limit: int | None = None) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        remaining = limit
        while remaining is None or remaining > 0:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def _data_rows(path: Path, byte_offset: int) -> int:
    if byte_offset == 0:
        return 0
    with path.open("rb") as handle:
        data = handle.read(byte_offset)
    lines = data.count(b"\n")
    return max(0, lines - 1) if path.suffix == ".csv" else lines


def _artifact_paths(run_dir: Path) -> list[Path]:
    excluded_files = {
        *APPEND_OUTPUTS,
        ".run.lock",
        "output_manifest.json",
        "resume_audit.jsonl",
    "lock_recovery_audit.jsonl",
        "run_progress.json",
    }
    paths = []
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(run_dir)
        if relative.parts[0] in {"checkpoints", "rollback"}:
            continue
        if relative.as_posix() in excluded_files:
            continue
        paths.append(path)
    return sorted(paths)


def build_output_manifest(
    run_dir: Path,
    transaction: int,
    last_completed_cycle: int,
    last_rpt_node: int | None,
    *,
    last_charge_efficiency_cycle: int | None = None,
    last_complete_soc_bin_cycle: int | None = None,
) -> OutputCommitManifest:
    append_files: dict[str, AppendFileCommit] = {}
    for name in APPEND_OUTPUTS:
        path = run_dir / name
        offset = path.stat().st_size if path.exists() else 0
        prefix_hash = _file_hash(path, offset) if offset else sha256(b"").hexdigest()
        append_files[name] = AppendFileCommit(name, offset, _data_rows(path, offset) if offset else 0, prefix_hash)
    artifacts = {}
    for path in _artifact_paths(run_dir):
        relative = path.relative_to(run_dir).as_posix()
        artifacts[relative] = ArtifactCommit(relative, path.stat().st_size, _file_hash(path))
    return OutputCommitManifest(
        transaction, append_files, artifacts, last_completed_cycle, last_rpt_node,
        last_charge_efficiency_cycle, last_complete_soc_bin_cycle,
    )


def write_output_manifest(path: Path, manifest: OutputCommitManifest, **audit: Any) -> None:
    write_json(path, {"commit": asdict(manifest), "audit": audit})


def save_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    _atomic_bytes(path, pickle.dumps(checkpoint, protocol=pickle.HIGHEST_PROTOCOL))


def load_checkpoint(
    path: Path,
    config: RunConfig,
    udds_fingerprint: str,
    *,
    input_fingerprint: str | None = None,
    initial_state_fingerprint: str | None = None,
    environment_fingerprint: str | None = None,
    effective_parameters_fingerprint: str = "",
    charge_efficiency_inventory_sha256: str = "",
) -> Checkpoint:
    with path.open("rb") as handle:
        checkpoint = pickle.load(handle)
    if isinstance(checkpoint, dict) and checkpoint.get("forensic_only"):
        raise TypeError("failure snapshot is forensic-only and not resume eligible")
    if not isinstance(checkpoint, Checkpoint):
        raise TypeError("checkpoint does not contain a W10 Checkpoint")
    if checkpoint.schema_version != config.checkpoint_schema_version:
        raise NumericalFailure(
            "UNSUPPORTED_CHECKPOINT_SCHEMA: "
            f"expected {config.checkpoint_schema_version}, got {checkpoint.schema_version}"
        )
    checks = {
        "configuration": (checkpoint.config_fingerprint, config.fingerprint()),
        "UDDS": (checkpoint.udds_fingerprint, udds_fingerprint),
        "input": (checkpoint.input_fingerprint, input_fingerprint),
        "initial state": (checkpoint.initial_state_fingerprint, initial_state_fingerprint),
        "environment": (checkpoint.environment_fingerprint, environment_fingerprint),
        "protocol algorithm": (checkpoint.protocol_algorithm_version, config.protocol_algorithm_version),
        "output schema": (checkpoint.output_schema_version, config.output_schema_version),
        "guard": (checkpoint.guard_config_fingerprint, config.guard_fingerprint()),
        "effective parameters": (
            checkpoint.effective_parameters_fingerprint,
            effective_parameters_fingerprint,
        ),
        "charge efficiency algorithm": (
            checkpoint.charge_efficiency_algorithm_version,
            config.charge_efficiency_algorithm_version,
        ),
        "solver execution": (
            checkpoint.solver_execution_version,
            config.solver_execution_version,
        ),
        "run context": (checkpoint.run_context_fingerprint, config.run_context_fingerprint or ""),
        "charge efficiency variable inventory": (
            checkpoint.charge_efficiency_variable_inventory_sha256,
            charge_efficiency_inventory_sha256,
        ),
    }
    for label, (actual, expected) in checks.items():
        if expected is not None and actual != expected:
            raise NumericalFailure(f"checkpoint {label} fingerprint does not match current run")
    if checkpoint.mode != config.mode:
        raise NumericalFailure("checkpoint RPT mode does not match current run")
    return checkpoint


def _archive_tail(path: Path, offset: int, archive: Path) -> int:
    size = path.stat().st_size
    if size <= offset:
        return 0
    archive.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as source:
        source.seek(offset)
        tail = source.read()
    _atomic_bytes(archive, tail)
    with path.open("r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())
    return size - offset


def rollback_to_checkpoint(
    run_dir: Path,
    checkpoint_path: Path,
    checkpoint: Checkpoint,
    *,
    _failure_after_actions: int | None = None,
) -> dict[str, Any]:
    """Validate committed prefixes, archive only post-boundary output, and truncate."""
    manifest = checkpoint.output_manifest
    for relative, commit in manifest.append_files.items():
        path = run_dir / relative
        if commit.byte_offset and not path.exists():
            raise NumericalFailure(f"committed output is missing: {relative}")
        size = path.stat().st_size if path.exists() else 0
        if size < commit.byte_offset:
            raise NumericalFailure(f"committed output is shorter than checkpoint: {relative}")
        if commit.byte_offset and _file_hash(path, commit.byte_offset) != commit.prefix_sha256:
            raise NumericalFailure(f"committed output prefix was modified: {relative}")
        if path.exists() and _data_rows(path, commit.byte_offset) != commit.data_rows:
            raise NumericalFailure(f"committed output row count differs: {relative}")
    for relative, commit in manifest.artifacts.items():
        path = run_dir / relative
        if not path.is_file() or path.stat().st_size != commit.size or _file_hash(path) != commit.sha256:
            raise NumericalFailure(f"committed artifact is missing or modified: {relative}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    archive_root = run_dir / "rollback" / timestamp
    truncated: dict[str, int] = {}
    moved: list[str] = []
    action_count = 0

    def failure_injection_point() -> None:
        nonlocal action_count
        action_count += 1
        if _failure_after_actions is not None and action_count >= _failure_after_actions:
            raise RuntimeError("injected rollback interruption")
    for relative, commit in manifest.append_files.items():
        path = run_dir / relative
        if path.exists() and path.stat().st_size > commit.byte_offset:
            count = _archive_tail(path, commit.byte_offset, archive_root / f"{relative}.uncommitted-tail")
            truncated[relative] = count
            failure_injection_point()
    committed_artifacts = set(manifest.artifacts)
    for path in _artifact_paths(run_dir):
        relative = path.relative_to(run_dir).as_posix()
        if relative not in committed_artifacts:
            destination = archive_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            moved.append(relative)
            failure_injection_point()
    for path in (run_dir / "checkpoints").glob("*"):
        if not path.is_file():
            continue
        if path.resolve() == checkpoint_path.resolve():
            continue
        keep_earlier = False
        if path.name.startswith("cycle-") and path.suffix == ".pkl":
            try:
                keep_earlier = int(path.stem.split("-")[1]) <= checkpoint.aging_cycle
            except (IndexError, ValueError):
                keep_earlier = False
        if not keep_earlier:
            relative = path.relative_to(run_dir).as_posix()
            destination = archive_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            moved.append(relative)
            failure_injection_point()
    audit = {
        "checkpoint": str(checkpoint_path.resolve()),
        "transaction": manifest.transaction,
        "truncated_bytes": truncated,
        "moved_files": moved,
        "rollback_archive": str(archive_root.relative_to(run_dir)) if (truncated or moved) else None,
        "performed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_output_manifest(
        run_dir / "output_manifest.json",
        manifest,
        checkpoint=checkpoint_path.name,
        rollback=audit,
    )
    append_json_line(run_dir / "resume_audit.jsonl", audit)
    return audit


def write_failure_artifacts(
    run_dir: Path,
    context: FailureContext,
    *,
    forensic_payload: Any | None = None,
) -> tuple[Path, Path]:
    """Atomically create non-resumable JSON and pickle failure evidence."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    cycle = "none" if context.cycle is None else f"{context.cycle:03d}"
    phase = "unknown" if context.phase is None else context.phase.value.lower()
    stem = f"failure-{timestamp}-cycle-{cycle}-{phase}"
    json_path = run_dir / "failures" / f"{stem}.json"
    pickle_path = run_dir / "failures" / f"{stem}.pkl"
    write_json(json_path, context.to_json())
    envelope = {
        "schema_version": 1,
        "forensic_only": True,
        "resume_eligible": False,
        "failure_context": context,
        "payload": forensic_payload,
    }
    _atomic_bytes(pickle_path, pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL))
    return json_path, pickle_path


def write_status(path: Path, status: RunStatus, **detail: Any) -> None:
    write_json(path, {"status": status.value, **detail})
