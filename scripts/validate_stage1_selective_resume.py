"""Validate the audited selective resume of the interrupted stage-1 fit."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import pickle
import sys
from typing import Any

import numpy as np


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "src"))

from pybamm_w10.backend import PyBaMMBackend
from pybamm_w10.calibration.aging import (
    Stage1AgingCalibration,
    _source_amendment_changes,
    _source_identity,
)
from pybamm_w10.calibration.artifacts import write_calibration_json
from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.calibration.surrogate import (
    PLATING_PROBE_ID,
    PLATING_PROBE_SCALE,
    RETIRED_PLATING_CANDIDATE_IDS,
    ProbeResponse,
    baseline_candidate,
    high_rate_candidates,
    mid_rate_candidates,
)
from pybamm_w10.charge_variables import preflight_charge_variables
from pybamm_w10.config import RunConfig
from pybamm_w10.model import (
    build_spme,
    certified_charge_solver_profile,
    conservative_charge_solver_profile,
)


STAGE_DIR = (
    WORKSPACE
    / "outputs"
    / "pybamm_spme_calibration"
    / "w10-stage1-soh-v1"
)
THRESHOLDS = {
    "external_charge_ah": 1e-3,
    "soc_at_charge_end_pct": 2e-2,
    "intercalated_charge_increment_ah": 5e-4,
    "reversible_plating_increment_ah": 5e-4,
    "dead_lithium_increment_ah": 5e-4,
    "sei_increment_ah": 5e-4,
}


def _candidate_config(candidate_dir: Path) -> RunConfig:
    saved = json.loads((candidate_dir / "run_config.json").read_text(encoding="utf-8"))
    return replace(
        RunConfig(
            mode=saved["mode"],
            data_root=Path(saved["data_root"]),
            output_root=candidate_dir,
            calibration_parameters_path=candidate_dir / "candidate_parameters.json",
        ),
        run_context_fingerprint=saved["run_context_fingerprint"],
    ).normalized(WORKSPACE)


def _summary(result: Any, checkpoint: Any, config: RunConfig) -> dict[str, float]:
    start = result.traces[0]
    end = result.traces[-1]
    external_charge_ah = sum(
        float(
            np.trapezoid(
                np.maximum(-np.asarray(trace.values["current_a"], dtype=float), 0.0),
                np.asarray(trace.time_s, dtype=float),
            )
            / 3600.0
        )
        for trace in result.traces
    )
    start_lithium = float(start.values["negative_particle_lithium_mol"][0])
    end_lithium = float(end.values["negative_particle_lithium_mol"][-1])
    intercalated = config.faraday_constant_c_per_mol * (end_lithium - start_lithium) / 3600.0
    return {
        "external_charge_ah": external_charge_ah,
        "soc_at_charge_end_pct": config.soc_anchor_pct + 100.0 * intercalated / checkpoint.q_ref_ah,
        "intercalated_charge_increment_ah": intercalated,
        "reversible_plating_increment_ah": float(
            end.values["reversible_plating_inventory_ah"][-1]
            - start.values["reversible_plating_inventory_ah"][0]
        ),
        "dead_lithium_increment_ah": float(
            end.values["dead_lithium_inventory_ah"][-1]
            - start.values["dead_lithium_inventory_ah"][0]
        ),
        "sei_increment_ah": float(
            end.values["cumulative_sei_loss_ah"][-1]
            - start.values["cumulative_sei_loss_ah"][0]
        ),
    }


def _solve(candidate_dir: Path, checkpoint_name: str, profile: Any) -> dict[str, Any]:
    config = _candidate_config(candidate_dir)
    parameters = load_calibration_parameters(candidate_dir / "candidate_parameters.json")
    artifacts = build_spme(config, parameters)
    inventory = preflight_charge_variables(artifacts.model, model_options=artifacts.options)
    with (candidate_dir / "checkpoints" / checkpoint_name).open("rb") as handle:
        checkpoint = pickle.load(handle)
    backend = PyBaMMBackend(artifacts, config.initial_soc)
    backend.restore(checkpoint.state)
    result = backend._solve_standard_charge_attempt(
        config, backend.snapshot(), profile, inventory, None
    )
    return {
        "profile": asdict(profile),
        "outcomes": [item.termination_kind.value for item in result.outcomes],
        "stage_durations_s": result.stage_durations_s,
        "terminal_state_hash": result.terminal_snapshot.state_hash,
        "summary": _summary(result, checkpoint, config),
    }


def _compare(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    differences = {
        key: abs(float(left["summary"][key]) - float(right["summary"][key]))
        for key in THRESHOLDS
    }
    passed = left["outcomes"] == right["outcomes"] and all(
        differences[key] <= limit for key, limit in THRESHOLDS.items()
    )
    return {
        "passed": passed,
        "outcomes_match": left["outcomes"] == right["outcomes"],
        "absolute_differences": differences,
        "limits": THRESHOLDS,
    }


def main() -> int:
    manifest = json.loads((STAGE_DIR / "stage1_manifest.json").read_text(encoding="utf-8"))
    current_source = _source_identity(WORKSPACE)
    changed = _source_amendment_changes(manifest["source_identity"], current_source)

    parameters = load_calibration_parameters(WORKSPACE / "inputs" / "spme_transferred_parameters.json")
    calibration = Stage1AgingCalibration(
        RunConfig(
            data_root=WORKSPACE / "data",
            calibration_parameters_path=WORKSPACE / "inputs" / "spme_transferred_parameters.json",
        ),
        WORKSPACE,
        STAGE_DIR,
        parameters,
        progress_writer=lambda message: None,
    )
    mids = {candidate.candidate_id: candidate for candidate in mid_rate_candidates()}
    highs = {
        "SEI-H": replace(mids["SEI-M"], candidate_id="SEI-H", scales=(10.0, 1.0, 1.0)),
        "LAM-H": replace(mids["LAM-M"], candidate_id="LAM-H", scales=(1.0, 1.0, 10.0)),
    }
    reused = {
        candidate.candidate_id: sorted(
            calibration._validated_reusable_candidate(candidate, 75) or {}
        )
        for candidate in (
            baseline_candidate(),
            mids["SEI-M"],
            mids["LAM-M"],
            highs["LAM-H"],
        )
    }
    reuse_passed = all(nodes == [0, 25, 75] for nodes in reused.values())

    baseline_response = ProbeResponse(baseline_candidate(), 99.0, 98.0)
    probe_responses = {
        "sei": ProbeResponse(mids["SEI-M"], 98.9, 97.5),
        "plating": ProbeResponse(
            mids[PLATING_PROBE_ID], 99.0, 98.0, numerically_censored=True
        ),
        "lam": ProbeResponse(mids["LAM-M"], 99.1, 97.5),
    }
    generated_highs = high_rate_candidates(
        baseline_response, probe_responses, {25: 99.0, 75: 97.0}
    )
    probe_plan_passed = (
        mids[PLATING_PROBE_ID].scales == (1.0, PLATING_PROBE_SCALE, 1.0)
        and all(item.candidate_id != "PLATING-H" for item in generated_highs)
        and not (STAGE_DIR / "candidates" / PLATING_PROBE_ID).exists()
        and all(
            (STAGE_DIR / "candidates" / candidate_id).is_dir()
            for candidate_id in RETIRED_PLATING_CANDIDATE_IDS
        )
    )

    plating_dir = STAGE_DIR / "candidates" / "PLATING-M"
    config = _candidate_config(plating_dir)
    primary = certified_charge_solver_profile(config)
    retry = conservative_charge_solver_profile(config)
    cross = replace(
        retry,
        name="certified_charge_crosscheck_bdf1",
        max_error_test_failures=200,
        max_order_bdf=1,
    )
    if not (
        primary.dt_init_s == retry.dt_init_s == cross.dt_init_s == 1e-8
        and primary.max_step_s == retry.max_step_s == cross.max_step_s == 1.0
    ):
        raise RuntimeError("resume compatibility profiles changed a solver step size")

    cycle50_primary = _solve(plating_dir, "cycle-049.pkl", primary)
    cycle50_retry = _solve(plating_dir, "cycle-049.pkl", retry)
    successful_cycle_compatibility = _compare(cycle50_primary, cycle50_retry)
    cycle51_retry = _solve(plating_dir, "cycle-050.pkl", retry)
    cycle51_cross = _solve(plating_dir, "cycle-050.pkl", cross)
    failed_cycle_convergence = _compare(cycle51_retry, cycle51_cross)

    passed = (
        reuse_passed
        and probe_plan_passed
        and successful_cycle_compatibility["passed"]
        and failed_cycle_convergence["passed"]
    )
    report = {
        "status": "PASSED" if passed else "FAILED",
        "original_run_context_fingerprint": manifest["run_context_fingerprint"],
        "original_source_sha256": manifest["source_identity"]["source_sha256"],
        "current_source_sha256": current_source["source_sha256"],
        "changed_source_files": list(changed),
        "completed_candidates_read_only_validation": {
            "passed": reuse_passed,
            "rpt_nodes": reused,
        },
        "replacement_plating_probe": {
            "passed": probe_plan_passed,
            "candidate_id": PLATING_PROBE_ID,
            "scales": [1.0, PLATING_PROBE_SCALE, 1.0],
            "starts_from_cycle": 0,
            "retired_candidate_ids": list(RETIRED_PLATING_CANDIDATE_IDS),
            "retired_directories_retained_for_audit": True,
            "plating_high_probe_disabled": True,
        },
        "step_size_policy": {
            "passed": True,
            "dt_init_s": 1e-8,
            "dt_max_s": 1.0,
            "note": "BDF order/error-test bounds change only; no step-size change",
        },
        "successful_cycle_compatibility": {
            **successful_cycle_compatibility,
            "primary": cycle50_primary,
            "retry": cycle50_retry,
        },
        "failed_cycle_convergence": {
            **failed_cycle_convergence,
            "retry": cycle51_retry,
            "crosscheck": cycle51_cross,
        },
    }
    write_calibration_json(STAGE_DIR / "resume_compatibility.json", report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
