"""Immutable configuration and reproducibility fingerprints."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal


RPT_NODES = (0, 25, 75, 122, 146, 148, 151, 159, 188, 225, 250, 275, 300, 325, 350)


@dataclass(frozen=True)
class CellConfig:
    nominal_capacity_ah: float = 4.85
    upper_cutoff_v: float = 4.2
    lower_cutoff_v: float = 2.5
    ambient_temperature_k: float = 296.15
    initial_temperature_k: float = 296.15
    mass_kg: float = 0.06925
    diameter_m: float = 0.02144
    length_m: float = 0.07080

    @property
    def volume_m3(self) -> float:
        from math import pi
        return pi * (self.diameter_m / 2) ** 2 * self.length_m

    @property
    def cooling_surface_area_m2(self) -> float:
        from math import pi
        radius = self.diameter_m / 2
        return 2 * pi * radius * self.length_m + 2 * pi * radius**2


@dataclass(frozen=True)
class ProtocolConfig:
    charge_3c_a: float = 14.55
    discharge_c4_a: float = 1.2125
    cv_cutoff_a: float = 0.05
    rpt_discharge_a: float = 0.24
    capacity_fraction_step5: float = 0.20
    capacity_fraction_window: float = 0.80
    rest_after_charge_s: float = 30 * 60
    rpt_rest_s: float = 60 * 60
    max_aging_cycles: int = 350
    rpt_nodes: tuple[int, ...] = RPT_NODES


@dataclass(frozen=True)
class SolverConfig:
    """Fixed numerical profiles; general values retain the legacy defaults."""

    rtol: float = 1e-5
    atol: float = 1e-7
    max_step_s: float = 1.0
    charge_rtol: float = 1e-5
    charge_atol: float = 1e-7
    charge_max_step_s: float = 1.0
    max_num_steps: int = 200_000
    root_tol: float = 1e-8

    def __post_init__(self) -> None:
        positive = (
            self.rtol,
            self.atol,
            self.max_step_s,
            self.charge_rtol,
            self.charge_atol,
            self.charge_max_step_s,
            self.root_tol,
        )
        if any(value <= 0 for value in positive) or self.max_num_steps <= 0:
            raise ValueError("solver tolerances, step limits, and work limit must be positive")


@dataclass(frozen=True)
class RunConfig:
    mode: Literal["virtual", "strict-w10"] = "virtual"
    initial_soc: float = 0.20
    cell: CellConfig = field(default_factory=CellConfig)
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    solver: SolverConfig = field(default_factory=SolverConfig)
    data_root: Path = Path("data")
    w10_mat_path: Path = Path("LG M50T/cycling/W10/W10-1.mat")
    output_root: Path = Path("outputs/pybamm_w10")
    protocol_algorithm_version: str = "w10-window-v3-charge-efficiency"
    output_schema_version: int = 3
    checkpoint_schema_version: int = 6
    charge_efficiency_algorithm_version: str = "charge-efficiency-v1"
    solver_execution_version: str = "stage-local-time-v2-robust-charge"
    solver_profile_policy_version: str = "phase-fixed-v1"
    solver_attempt_audit_version: str = "solver-attempt-v1"
    soc_definition: str = "NEGATIVE_PARTICLE_LITHIUM_DELTA_OVER_FROZEN_Q_REF_V1"
    soc_anchor_pct: float = 20.0
    soc_boundaries_pct: tuple[float, ...] = (20.0, 40.0, 60.0, 80.0, 100.0)
    soc_boundary_residual_tolerance_pct: float = 1e-6
    soc_nonmonotonic_tolerance_pct: float = 1e-6
    charge_balance_pass_limit_pct: float = 0.2
    charge_balance_failure_limit_pct: float = 1.0
    plating_crosscheck_abs_tolerance_ah: float = 1e-8
    plating_crosscheck_relative_tolerance: float = 1e-5
    faraday_constant_c_per_mol: float = 96485.33212
    charge_integration_method: str = "STAGE_LOCAL_TRAPEZOID_WITH_EXACT_SOC_BOUNDARIES_V1"
    soc_crossing_selection_rule: str = "FIRST_UPWARD_CROSSING"
    udds_event_guard_fraction: float = 0.005
    udds_event_guard_solver_steps: int = 10
    heartbeat_interval_s: int = 60
    checkpoint_every_cycles: int = 1
    capacity_window_relative_tolerance: float = 0.001
    retained_timeseries_cycles: tuple[int, ...] = (1, 25, 75, 175, 350)
    udds_period_min_s: int = 2400
    udds_period_max_s: int = 2800
    required_python: Path = Path("C:/Users/Lenovo/anaconda3/envs/battery/python.exe")
    calibration_parameters_path: Path | None = None
    run_context_fingerprint: str | None = None

    @property
    def cycling_root(self) -> Path:
        """Read-only root for cycling MAT/CSV inputs beneath ``data_root``."""
        return self.data_root / "LG M50T/cycling"

    @property
    def capacity_diagnostics_root(self) -> Path:
        """Read-only root for processed capacity-diagnostic inputs."""
        return self.data_root / "LG M50T/_processed_mat"

    def __post_init__(self) -> None:
        if not all((self.protocol_algorithm_version, self.charge_efficiency_algorithm_version,
                    self.solver_execution_version, self.solver_profile_policy_version,
                    self.solver_attempt_audit_version,
                    self.soc_definition, self.charge_integration_method,
                    self.soc_crossing_selection_rule)):
            raise ValueError("algorithm and SOC version identifiers must be non-empty")
        if self.output_schema_version < 1 or self.checkpoint_schema_version < 1:
            raise ValueError("schema versions must be positive")
        if self.udds_event_guard_fraction <= 0 or self.udds_event_guard_solver_steps <= 0:
            raise ValueError("UDDS guard configuration must be positive")
        if self.heartbeat_interval_s <= 0 or self.checkpoint_every_cycles <= 0:
            raise ValueError("heartbeat and checkpoint intervals must be positive")
        if self.capacity_window_relative_tolerance <= 0:
            raise ValueError("capacity window tolerance must be positive")
        if self.soc_boundaries_pct != (20.0, 40.0, 60.0, 80.0, 100.0):
            raise ValueError("SOC boundaries must be the fixed 20/40/60/80/100 percent set")
        if any(right <= left for left, right in zip(self.soc_boundaries_pct, self.soc_boundaries_pct[1:])):
            raise ValueError("SOC boundaries must be strictly increasing")
        positive = (
            self.soc_boundary_residual_tolerance_pct,
            self.soc_nonmonotonic_tolerance_pct,
            self.plating_crosscheck_abs_tolerance_ah,
            self.plating_crosscheck_relative_tolerance,
            self.faraday_constant_c_per_mol,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("charge-efficiency tolerances and Faraday constant must be positive")
        if not self.charge_balance_pass_limit_pct < self.charge_balance_failure_limit_pct:
            raise ValueError("charge balance pass limit must be less than failure limit")
        if self.run_context_fingerprint is not None:
            if len(self.run_context_fingerprint) != 64:
                raise ValueError("run_context_fingerprint must be a SHA-256 digest")
            int(self.run_context_fingerprint, 16)

    def normalized(self, workspace: Path) -> "RunConfig":
        workspace = workspace.resolve()
        data_root = self.data_root if self.data_root.is_absolute() else workspace / self.data_root
        data_root = data_root.resolve()
        w10_mat_path = (
            self.w10_mat_path.resolve()
            if self.w10_mat_path.is_absolute()
            else (data_root / self.w10_mat_path).resolve()
        )
        calibration_parameters_path = (
            None
            if self.calibration_parameters_path is None
            else (
                self.calibration_parameters_path.resolve()
                if self.calibration_parameters_path.is_absolute()
                else (workspace / self.calibration_parameters_path).resolve()
            )
        )
        return RunConfig(
            mode=self.mode,
            initial_soc=self.initial_soc,
            cell=self.cell,
            protocol=self.protocol,
            solver=self.solver,
            data_root=data_root,
            w10_mat_path=w10_mat_path,
            output_root=(workspace / self.output_root).resolve(),
            protocol_algorithm_version=self.protocol_algorithm_version,
            output_schema_version=self.output_schema_version,
            checkpoint_schema_version=self.checkpoint_schema_version,
            charge_efficiency_algorithm_version=self.charge_efficiency_algorithm_version,
            solver_execution_version=self.solver_execution_version,
            solver_profile_policy_version=self.solver_profile_policy_version,
            solver_attempt_audit_version=self.solver_attempt_audit_version,
            soc_definition=self.soc_definition,
            soc_anchor_pct=self.soc_anchor_pct,
            soc_boundaries_pct=self.soc_boundaries_pct,
            soc_boundary_residual_tolerance_pct=self.soc_boundary_residual_tolerance_pct,
            soc_nonmonotonic_tolerance_pct=self.soc_nonmonotonic_tolerance_pct,
            charge_balance_pass_limit_pct=self.charge_balance_pass_limit_pct,
            charge_balance_failure_limit_pct=self.charge_balance_failure_limit_pct,
            plating_crosscheck_abs_tolerance_ah=self.plating_crosscheck_abs_tolerance_ah,
            plating_crosscheck_relative_tolerance=self.plating_crosscheck_relative_tolerance,
            faraday_constant_c_per_mol=self.faraday_constant_c_per_mol,
            charge_integration_method=self.charge_integration_method,
            soc_crossing_selection_rule=self.soc_crossing_selection_rule,
            udds_event_guard_fraction=self.udds_event_guard_fraction,
            udds_event_guard_solver_steps=self.udds_event_guard_solver_steps,
            heartbeat_interval_s=self.heartbeat_interval_s,
            checkpoint_every_cycles=self.checkpoint_every_cycles,
            capacity_window_relative_tolerance=self.capacity_window_relative_tolerance,
            retained_timeseries_cycles=self.retained_timeseries_cycles,
            udds_period_min_s=self.udds_period_min_s,
            udds_period_max_s=self.udds_period_max_s,
            required_python=self.required_python.resolve(),
            calibration_parameters_path=calibration_parameters_path,
            run_context_fingerprint=self.run_context_fingerprint,
        )

    def fingerprint(self) -> str:
        payload = asdict(self)
        payload["data_root"] = str(payload["data_root"])
        payload["w10_mat_path"] = str(payload["w10_mat_path"])
        payload["output_root"] = str(payload["output_root"])
        payload["required_python"] = str(payload["required_python"])
        payload["calibration_parameters_path"] = (
            None if payload["calibration_parameters_path"] is None else str(payload["calibration_parameters_path"])
        )
        payload["run_context_fingerprint"] = self.run_context_fingerprint
        return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def guard_fingerprint(self) -> str:
        payload = {
            "protocol_algorithm_version": self.protocol_algorithm_version,
            "udds_event_guard_fraction": self.udds_event_guard_fraction,
            "udds_event_guard_solver_steps": self.udds_event_guard_solver_steps,
            "solver_max_step_s": self.solver.max_step_s,
            "solver_rtol": self.solver.rtol,
            "solver_atol": self.solver.atol,
            "charge_solver_rtol": self.solver.charge_rtol,
            "charge_solver_atol": self.solver.charge_atol,
            "charge_solver_max_step_s": self.solver.charge_max_step_s,
            "solver_profile_policy_version": self.solver_profile_policy_version,
            "capacity_window_relative_tolerance": self.capacity_window_relative_tolerance,
        }
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def to_json(self) -> dict[str, object]:
        value = asdict(self)
        value["data_root"] = str(value["data_root"])
        value["w10_mat_path"] = str(value["w10_mat_path"])
        value["output_root"] = str(value["output_root"])
        value["required_python"] = str(value["required_python"])
        value["calibration_parameters_path"] = (
            None if value["calibration_parameters_path"] is None else str(value["calibration_parameters_path"])
        )
        value["run_context_fingerprint"] = self.run_context_fingerprint
        return value
