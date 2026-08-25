"""PyBaMM SPMe construction; no module-level model or solver side effects."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from .config import RunConfig
from .calibration.parameters import CalibrationParameters, apply_calibration_parameters
from .types import SolverProfile


@dataclass
class ModelArtifacts:
    model: Any
    parameter_values: Any
    solver: Any
    charge_solver: Any
    options: dict[str, object]


def aging_options() -> dict[str, object]:
    """The exact degradation option set mandated by the approved specification."""
    return {
        "thermal": "lumped",
        "SEI": "solvent-diffusion limited",
        "SEI porosity change": "true",
        "SEI on cracks": "true",
        "lithium plating": "partially reversible",
        "lithium plating porosity change": "true",
        "particle mechanics": ("swelling and cracking", "swelling only"),
        "loss of active material": "stress-driven",
        "stress-induced diffusion": "true",
        "x-average side reactions": "false",
    }


def default_solver_profile(config: RunConfig) -> SolverProfile:
    return SolverProfile(
        name="general_protocol",
        rtol=config.solver.rtol,
        atol=config.solver.atol,
        dt_init_s=0.0,
        max_step_s=config.solver.max_step_s,
        max_num_steps=config.solver.max_num_steps,
        max_error_test_failures=10,
        max_order_bdf=5,
        suppress_algebraic_error=False,
    )


def conservative_solver_profile(config: RunConfig) -> SolverProfile:
    return SolverProfile(
        name="conservative_cv_transition",
        rtol=config.solver.rtol,
        atol=config.solver.atol,
        dt_init_s=1e-8,
        max_step_s=config.solver.max_step_s,
        max_num_steps=config.solver.max_num_steps,
        max_error_test_failures=30,
        max_order_bdf=3,
        suppress_algebraic_error=True,
    )


def certified_charge_solver_profile(config: RunConfig) -> SolverProfile:
    """The fixed CC/CV charge profile; values are configured in ``SolverConfig``."""
    return SolverProfile(
        name="certified_charge",
        rtol=config.solver.charge_rtol,
        atol=config.solver.charge_atol,
        dt_init_s=0.0,
        max_step_s=config.solver.charge_max_step_s,
        max_num_steps=config.solver.max_num_steps,
        max_error_test_failures=10,
        max_order_bdf=5,
        suppress_algebraic_error=False,
    )


def conservative_charge_solver_profile(config: RunConfig) -> SolverProfile:
    """Audited retry retaining the certified charge tolerances and step cap."""
    certified = certified_charge_solver_profile(config)
    return SolverProfile(
        name="certified_charge_retry",
        rtol=certified.rtol,
        atol=certified.atol,
        dt_init_s=1e-8,
        max_step_s=certified.max_step_s,
        max_num_steps=certified.max_num_steps,
        max_error_test_failures=30,
        max_order_bdf=3,
        suppress_algebraic_error=True,
    )


def build_solver(config: RunConfig, profile: SolverProfile | None = None) -> Any:
    import pybamm

    selected = profile or default_solver_profile(config)
    return pybamm.IDAKLUSolver(
        rtol=selected.rtol,
        atol=selected.atol,
        root_tol=config.solver.root_tol,
        on_failure="error",
        on_extrapolation="error",
        options={
            "max_num_steps": selected.max_num_steps,
            "dt_init": selected.dt_init_s,
            "dt_max": selected.max_step_s,
            "max_error_test_failures": selected.max_error_test_failures,
            "max_order_bdf": selected.max_order_bdf,
            "suppress_algebraic_error": selected.suppress_algebraic_error,
        },
    )


def build_spme(
    config: RunConfig, calibration_parameters: CalibrationParameters | None = None
) -> ModelArtifacts:
    """Build the OKane2022 SPMe and only the experimentally justified overrides.

    This function constructs objects only. It intentionally does not call
    ``Simulation.solve`` or start an experiment.
    """
    import pybamm

    options = aging_options()
    model = pybamm.lithium_ion.SPMe(options=options)
    parameters = pybamm.ParameterValues("OKane2022")
    cell = config.cell
    parameters.update(
        {
            "Nominal cell capacity [A.h]": cell.nominal_capacity_ah,
            "Upper voltage cut-off [V]": cell.upper_cutoff_v,
            "Lower voltage cut-off [V]": cell.lower_cutoff_v,
            "Ambient temperature [K]": cell.ambient_temperature_k,
            "Initial temperature [K]": cell.initial_temperature_k,
            "Cell volume [m3]": cell.volume_m3,
            "Cell cooling surface area [m2]": cell.cooling_surface_area_m2,
        },
    )
    if calibration_parameters is not None:
        apply_calibration_parameters(parameters, calibration_parameters)
    solver = build_solver(config)
    charge_solver = build_solver(config, certified_charge_solver_profile(config))
    return ModelArtifacts(
        model=model,
        parameter_values=parameters,
        solver=solver,
        charge_solver=charge_solver,
        options=options,
    )


def _numeric_parameter(parameters: Any, name: str) -> float:
    """Extract a serialisable scalar from a PyBaMM parameter set."""
    value = parameters[name]
    return float(value)


def effective_parameters_fingerprint(audit: dict[str, object]) -> str:
    """Fingerprint audit content without making its self-hash recursive."""
    payload = {key: value for key, value in audit.items() if key != "fingerprint"}
    return sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def effective_parameters_audit(
    artifacts: ModelArtifacts,
    config: RunConfig,
    *,
    cycle_0_capacity_ah: float | None = None,
    calibration_parameters: CalibrationParameters | None = None,
) -> dict[str, object]:
    """Describe original and applied values without mutating the model.

    The cycle-0 capacity remains unset until the initial RPT succeeds.  Future
    calibration phases can replace only the explicitly named calibration slots.
    """
    import pybamm

    original = pybamm.ParameterValues("OKane2022")
    effective = artifacts.parameter_values

    def entry(name: str, source: str) -> dict[str, object]:
        return {
            "parameter": name,
            "original": _numeric_parameter(original, name),
            "effective": _numeric_parameter(effective, name),
            "source": source,
        }

    parameters = {
        "nominal_cell_capacity_ah": entry("Nominal cell capacity [A.h]", "m50t_experimental_override"),
        "upper_cutoff_voltage_v": entry("Upper voltage cut-off [V]", "m50t_experimental_override"),
        "lower_cutoff_voltage_v": entry("Lower voltage cut-off [V]", "m50t_experimental_override"),
        "ambient_temperature_k": entry("Ambient temperature [K]", "m50t_experimental_override"),
        "initial_temperature_k": entry("Initial temperature [K]", "m50t_experimental_override"),
        "cell_volume_m3": entry("Cell volume [m3]", "m50t_experimental_override"),
        "cell_cooling_surface_area_m2": entry("Cell cooling surface area [m2]", "m50t_experimental_override"),
        "electrode_width_m": entry("Electrode width [m]", "okane2022_original"),
        "electrode_height_m": entry("Electrode height [m]", "okane2022_original"),
        "negative_electrode_thickness_m": entry("Negative electrode thickness [m]", "okane2022_original"),
        "positive_electrode_thickness_m": entry("Positive electrode thickness [m]", "okane2022_original"),
    }

    def electrode_window(label: str) -> dict[str, float]:
        thickness = _numeric_parameter(effective, f"{label} electrode thickness [m]")
        active_fraction = _numeric_parameter(effective, f"{label} electrode active material volume fraction")
        maximum_concentration = _numeric_parameter(
            effective, f"Maximum concentration in {label.lower()} electrode [mol.m-3]"
        )
        area = _numeric_parameter(effective, "Electrode width [m]") * _numeric_parameter(
            effective, "Electrode height [m]"
        )
        theoretical_ah = pybamm.constants.F.value * maximum_concentration * active_fraction * thickness * area / 3600
        initial_concentration = _numeric_parameter(effective, f"Initial concentration in {label.lower()} electrode [mol.m-3]")
        return {
            "theoretical_capacity_ah": float(theoretical_ah),
            "active_material_volume_fraction": active_fraction,
            "maximum_concentration_mol_m3": maximum_concentration,
            "initial_stoichiometry": initial_concentration / maximum_concentration,
            "minimum_stoichiometry": 0.0,
            "maximum_stoichiometry": 1.0,
        }

    audit: dict[str, object] = {
        "audit_schema_version": 1,
        "original_parameter_set": "OKane2022",
        "model": "SPMe",
        "parameters": parameters,
        "external_cylindrical_geometry": {
            "diameter_m": config.cell.diameter_m,
            "length_m": config.cell.length_m,
            "mass_kg": config.cell.mass_kg,
            "source": "m50t_experimental_override",
        },
        "theoretical_capacity_window_ah": {
            "negative": electrode_window("Negative"),
            "positive": electrode_window("Positive"),
        },
        "stoichiometry_endpoints": {
            "negative": {key: electrode_window("Negative")[key] for key in ("initial_stoichiometry", "minimum_stoichiometry", "maximum_stoichiometry")},
            "positive": {key: electrode_window("Positive")[key] for key in ("initial_stoichiometry", "minimum_stoichiometry", "maximum_stoichiometry")},
        },
        "calibration": {
            name: {
                "value": None if calibration_parameters is None and name == "capacity_scale_factor" else (
                    1.0 if calibration_parameters is None else calibration_parameters.values[name]
                ),
                "allowed": True,
                "source": "not_calibrated" if calibration_parameters is None else calibration_parameters.degradation_parameter_status,
            }
            for name in ("capacity_scale_factor", "sei_scale", "plating_scale", "lam_scale")
        },
        "calibration_source_model": (
            None if calibration_parameters is None else calibration_parameters.source_model
        ),
        "rpt": {"cycle_0_capacity_ah": cycle_0_capacity_ah},
    }
    audit["fingerprint"] = effective_parameters_fingerprint(audit)
    return audit


def environment_metadata(artifacts: ModelArtifacts) -> dict[str, object]:
    import platform
    import sys
    import casadi
    import numpy
    import pybamm

    return {
        "python": sys.version,
        "pybamm": pybamm.__version__,
        "numpy": numpy.__version__,
        "casadi": casadi.__version__,
        "platform": platform.platform(),
        "solver": type(artifacts.solver).__name__,
        "model": artifacts.model.name,
        "model_class": type(artifacts.model).__name__,
        "solver_options": artifacts.solver.options,
    }
