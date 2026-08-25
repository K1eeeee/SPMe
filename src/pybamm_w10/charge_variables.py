"""Resolve and document the PyBaMM variables used by charge analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .types import FailureContext, FailureReason, NumericalFailure


@dataclass(frozen=True)
class ChargeVariableRole:
    key: str
    candidate_names: tuple[str, ...]
    unit: str
    declared_shape: str
    aggregation: str
    required_for_core: bool = False
    required_for_mechanism_analysis: bool = False
    not_applicable_reason: str | None = None


@dataclass(frozen=True)
class ResolvedChargeVariables:
    model_name: str
    pybamm_version: str
    model_options_fingerprint: str
    roles: tuple[tuple[ChargeVariableRole, str | None], ...]
    core_preflight_passed: bool
    mechanism_preflight_passed: bool

    def resolved_name(self, key: str) -> str:
        for role, name in self.roles:
            if role.key == key and name is not None:
                return name
        raise KeyError(key)

    def to_json(self) -> dict[str, object]:
        roles = {
            role.key: {
                **asdict(role),
                "candidate_names": list(role.candidate_names),
                "resolved_name": name,
                "available": name is not None,
            }
            for role, name in self.roles
        }
        return {
            "inventory_schema_version": 1,
            "charge_efficiency_algorithm_version": "charge-efficiency-v1",
            "model_name": self.model_name,
            "pybamm_version": self.pybamm_version,
            "model_options_fingerprint": self.model_options_fingerprint,
            "roles": roles,
            "core_preflight_passed": self.core_preflight_passed,
            "mechanism_preflight_passed": self.mechanism_preflight_passed,
        }


@dataclass(frozen=True)
class PlatingInventory:
    total_plating_inventory_ah: float
    dead_lithium_inventory_ah: float
    reversible_plating_inventory_ah: float
    reversible_plating_inventory_crosscheck_ah: float


def _role(
    key: str, name: str, unit: str, shape: str, aggregation: str, *, core: bool = False,
    mechanism: bool = False, aliases: tuple[str, ...] = (),
) -> ChargeVariableRole:
    return ChargeVariableRole(key, (name, *aliases), unit, shape, aggregation, core, mechanism)


CHARGE_VARIABLE_ROLES = (
    _role("time_s", "Time [s]", "s", "scalar", "stage_time", core=True),
    _role("current_a", "Current [A]", "A", "scalar", "stage_local_trapezoid_max_negative", core=True),
    _role("terminal_voltage_v", "Terminal voltage [V]", "V", "scalar", "time_weighted_mean_and_max", core=True),
    _role("temperature_k", "X-averaged cell temperature [K]", "K", "scalar", "time_weighted_mean_and_max", core=True),
    _role(
        "negative_electrode_surface_potential_difference_v",
        "Negative electrode surface potential difference [V]",
        "V",
        "space_field",
        "spatiotemporal_min",
        mechanism=True,
    ),
    _role("negative_particle_lithium_mol", "Total lithium in negative electrode [mol]", "mol", "scalar", "endpoint_delta", core=True),
    _role("total_plating_inventory_ah", "Loss of capacity to negative lithium plating [A.h]", "A.h", "scalar", "endpoint_delta", core=True),
    _role("dead_lithium_concentration_mol_m3", "Volume-averaged negative dead lithium concentration [mol.m-3]", "mol.m-3", "scalar", "endpoint_inventory", core=True),
    _role("reversible_plating_concentration_mol_m3", "Volume-averaged negative lithium plating concentration [mol.m-3]", "mol.m-3", "scalar", "geometric_crosscheck", core=True),
    _role("negative_sei_inventory_ah", "Loss of capacity to negative SEI [A.h]", "A.h", "scalar", "endpoint_delta", core=True),
    _role("negative_sei_cracks_inventory_ah", "Loss of capacity to negative SEI on cracks [A.h]", "A.h", "scalar", "endpoint_delta", core=True),
    _role("negative_surface_stoichiometry", "X-averaged negative particle surface stoichiometry", "1", "scalar", "endpoint", mechanism=True),
    _role("negative_average_stoichiometry", "X-averaged negative particle stoichiometry", "1", "scalar", "endpoint", mechanism=True, aliases=("R-averaged negative particle stoichiometry",)),
    _role("electrolyte_concentration_mol_m3", "Electrolyte concentration [mol.m-3]", "mol.m-3", "space_field", "spatiotemporal_min", mechanism=True),
    _role("negative_reaction_overpotential_v", "X-averaged negative electrode reaction overpotential [V]", "V", "scalar", "time_weighted_mean", mechanism=True),
    _role("plating_reaction_overpotential_v", "X-averaged negative electrode lithium plating reaction overpotential [V]", "V", "scalar", "raw_sign_most_adverse_extreme", mechanism=True),
    _role("negative_intercalation_current_density_a_m2", "X-averaged negative electrode interfacial current density [A.m-2]", "A.m-2", "scalar", "time_weighted_mean_and_max", mechanism=True),
    _role("negative_plating_current_density_a_m2", "X-averaged negative electrode lithium plating interfacial current density [A.m-2]", "A.m-2", "scalar", "time_weighted_mean_and_extreme", mechanism=True),
    _role("negative_sei_current_density_a_m2", "X-averaged negative electrode SEI interfacial current density [A.m-2]", "A.m-2", "scalar", "sum_with_cracks_then_time_weighted", mechanism=True),
    _role("negative_sei_cracks_current_density_a_m2", "X-averaged negative electrode SEI on cracks interfacial current density [A.m-2]", "A.m-2", "scalar", "sum_with_standard_sei_then_time_weighted", mechanism=True),
    _role("electrolyte_ohmic_loss_v", "X-averaged battery electrolyte ohmic losses [V]", "V", "scalar", "time_weighted_mean_and_max", mechanism=True),
    _role("negative_particle_concentration_overpotential_v", "Battery negative particle concentration overpotential [V]", "V", "scalar", "time_weighted_mean_and_max", mechanism=True),
    _role("irreversible_heating_w", "Irreversible electrochemical heating [W]", "W", "scalar", "time_integral_wh", mechanism=True),
    _role("ohmic_heating_w", "Ohmic heating [W]", "W", "scalar", "time_integral_wh", mechanism=True),
    _role("reversible_heating_w", "Reversible heating [W]", "W", "scalar", "time_integral_wh", mechanism=True),
    _role("total_heating_w", "Total heating [W]", "W", "scalar", "time_integral_wh", mechanism=True),
    _role("lli_pct", "Loss of lithium inventory [%]", "%", "scalar", "interval_start", mechanism=True),
    _role("negative_lam_pct", "LAM_ne [%]", "%", "scalar", "interval_start", mechanism=True, aliases=("Loss of active material in negative electrode [%]",)),
    _role("positive_lam_pct", "LAM_pe [%]", "%", "scalar", "interval_start", mechanism=True, aliases=("Loss of active material in positive electrode [%]",)),
)


def _stable_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def preflight_charge_variables(model: Any, *, model_options: dict[str, object]) -> ResolvedChargeVariables:
    """Resolve all mandatory roles before the first aging cycle starts."""
    available = model.variables
    resolved = tuple((role, next((name for name in role.candidate_names if name in available), None)) for role in CHARGE_VARIABLE_ROLES)
    missing = [role.key for role, name in resolved if name is None and (role.required_for_core or role.required_for_mechanism_analysis)]
    if missing:
        raise NumericalFailure(
            FailureContext(
                reason=FailureReason.MISSING_MODEL_VARIABLE,
                message=f"MISSING_MODEL_VARIABLE: {', '.join(missing)}",
            )
        )
    try:
        import pybamm
        pybamm_version = pybamm.__version__
    except ImportError:  # Allows deterministic fake-model unit tests.
        pybamm_version = "unavailable"
    return ResolvedChargeVariables(
        model_name=str(getattr(model, "name", type(model).__name__)),
        pybamm_version=pybamm_version,
        model_options_fingerprint=_stable_hash(model_options),
        roles=resolved,
        core_preflight_passed=True,
        mechanism_preflight_passed=True,
    )


def write_charge_efficiency_variable_inventory(path: Path, inventory: ResolvedChargeVariables) -> Path:
    """Write the inventory atomically and include a self-hash excluding itself."""
    payload = inventory.to_json()
    payload["inventory_sha256"] = _stable_hash(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return path


def negative_electrode_volume_m3(thickness_m: float, width_m: float, height_m: float) -> float:
    volume = thickness_m * width_m * height_m
    if volume <= 0:
        raise ValueError("negative electrode volume must be positive")
    return volume


def plating_inventories_ah(
    total_plating_inventory_ah: float,
    dead_lithium_concentration_mol_m3: float,
    reversible_plating_concentration_mol_m3: float,
    negative_electrode_volume: float,
    faraday_constant_c_per_mol: float = 96485.33212,
) -> PlatingInventory:
    dead = dead_lithium_concentration_mol_m3 * negative_electrode_volume * faraday_constant_c_per_mol / 3600.0
    crosscheck = reversible_plating_concentration_mol_m3 * negative_electrode_volume * faraday_constant_c_per_mol / 3600.0
    return PlatingInventory(total_plating_inventory_ah, dead, total_plating_inventory_ah - dead, crosscheck)
