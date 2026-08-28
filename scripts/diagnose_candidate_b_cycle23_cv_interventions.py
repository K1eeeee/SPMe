"""Counterfactual checks for the candidate-B cycle-23 CV failure."""

from __future__ import annotations

from dataclasses import replace
import json
import pickle

from diagnose_candidate_b_cycle23_cv import (
    CANDIDATE_DIR,
    CHECKPOINT_PATH,
    WORKSPACE,
    _config,
    _run_prefix,
)

from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.model import (
    build_spme,
    certified_charge_solver_profile,
    conservative_charge_solver_profile,
)


OUTPUT_PATH = (
    CANDIDATE_DIR
    / "diagnostics"
    / "cycle-023-4v-cv-counterfactual-interventions.json"
)


def main() -> int:
    config = _config()
    parameters = load_calibration_parameters(CANDIDATE_DIR / "candidate_parameters.json")
    with CHECKPOINT_PATH.open("rb") as handle:
        checkpoint = pickle.load(handle)

    report: dict[str, object] = {
        "diagnostic_only": True,
        "source_checkpoint": str(CHECKPOINT_PATH),
        "source_state_hash": checkpoint.state.state_hash,
        "warning": (
            "Parameter interventions retain the aged state and change only the next "
            "charge kinetics; they are causal diagnostics, not certified production runs."
        ),
        "plating_scale_interventions": [],
        "cv_cutoff_interventions": [],
        "algebraic_error_control_interventions": [],
    }

    for plating_scale in (1.5, 1.0, 0.5):
        modified = replace(parameters, plating_scale=plating_scale)
        artifacts = build_spme(config, modified)
        for profile in (
            certified_charge_solver_profile(config),
            conservative_charge_solver_profile(config),
        ):
            result = _run_prefix(
                artifacts,
                config,
                checkpoint,
                profile,
                None,
            )
            result["plating_scale"] = plating_scale
            report["plating_scale_interventions"].append(result)

    baseline_artifacts = build_spme(config, parameters)
    for cutoff_a in (0.2, 0.15, 0.12, 0.1):
        modified_config = replace(
            config,
            protocol=replace(config.protocol, cv_cutoff_a=cutoff_a),
        )
        for profile in (
            certified_charge_solver_profile(modified_config),
            conservative_charge_solver_profile(modified_config),
        ):
            result = _run_prefix(
                baseline_artifacts,
                modified_config,
                checkpoint,
                profile,
                None,
            )
            result["cv_cutoff_a"] = cutoff_a
            report["cv_cutoff_interventions"].append(result)

    for profile in (
        replace(
            certified_charge_solver_profile(config),
            name="diagnostic_bdf3_include_algebraic_error",
            suppress_algebraic_error=False,
        ),
        replace(
            conservative_charge_solver_profile(config),
            name="diagnostic_bdf2_include_algebraic_error",
            suppress_algebraic_error=False,
        ),
    ):
        report["algebraic_error_control_interventions"].append(
            _run_prefix(
                baseline_artifacts,
                config,
                checkpoint,
                profile,
                None,
            )
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
