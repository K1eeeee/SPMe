"""Diagnostic-only replay of cycle-10 standard charge from a legacy checkpoint."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import gc
import pickle
from pathlib import Path
import traceback

from pybamm_w10.backend import PyBaMMBackend
from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.charge_efficiency import build_charge_analysis_bundle
from pybamm_w10.charge_variables import preflight_charge_variables
from pybamm_w10.config import RunConfig
from pybamm_w10.model import build_spme
from pybamm_w10.output import write_json


def _legacy_attempt(config: RunConfig, calibration, state) -> dict[str, object]:
    artifacts = build_spme(config, calibration)
    backend = PyBaMMBackend(artifacts, config.initial_soc)
    backend.restore(state)
    try:
        backend.cc_charge_to_voltage(config.protocol.charge_3c_a, 4.0)
        backend.cv_hold_to_current(4.0, config.protocol.cv_cutoff_a)
    except Exception as exc:
        return {
            "status": "FAILED",
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    return {"status": "COMPLETED", "end_time_s": backend.current_time_s()}


def _continuous_attempt(
    config: RunConfig,
    calibration,
    state,
    *,
    q_ref_ah: float,
    q_ref_node: int,
    q_ref_initial_ah: float,
) -> dict[str, object]:
    artifacts = build_spme(config, calibration)
    inventory = preflight_charge_variables(artifacts.model, model_options=artifacts.options)
    backend = PyBaMMBackend(artifacts, config.initial_soc)
    backend.restore(state)
    result = backend.run_standard_charge_sequence(config, resolved_variables=inventory)
    analysis = build_charge_analysis_bundle(
        result.traces,
        cycle=10,
        mode=config.mode,
        q_ref_ah=q_ref_ah,
        q_ref_node=q_ref_node,
        q_ref_initial_ah=q_ref_initial_ah,
        configured_charge_current_a=config.protocol.charge_3c_a,
        nominal_capacity_ah=config.cell.nominal_capacity_ah,
        faraday_constant_c_per_mol=config.faraday_constant_c_per_mol,
        soc_anchor_pct=config.soc_anchor_pct,
        balance_pass_limit_pct=config.charge_balance_pass_limit_pct,
        balance_failure_limit_pct=config.charge_balance_failure_limit_pct,
    )
    output_times = tuple(float(row["time_s"]) for row in analysis.trace_rows)
    return {
        "status": "COMPLETED",
        "attempt_count": result.attempt_count,
        "solver_profile": result.solver_profile,
        "initial_failure_code": result.initial_failure_code,
        "termination_kinds": [outcome.termination_kind.value for outcome in result.outcomes],
        "stage_durations_s": dict(result.stage_durations_s),
        "terminal_state_hash": result.terminal_snapshot.state_hash,
        "trace_point_counts": [len(trace.time_s) for trace in result.traces],
        "charge_analysis_status": analysis.status.primary_status.value,
        "external_charge_ah": analysis.summary.values["external_charge_ah"],
        "analysis_point_count": analysis.summary.values["charge_integration_point_count"],
        "output_time_strictly_increasing": all(
            right > left for left, right in zip(output_times, output_times[1:])
        ),
        "attempt_failures": [asdict(item) for item in result.attempt_failures],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--calibration-params", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-legacy", action="store_true")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    checkpoint_path = args.checkpoint.resolve()
    output_path = args.output.resolve()
    if workspace not in output_path.parents:
        raise ValueError("diagnostic output must remain inside the workspace")
    if workspace in checkpoint_path.parents and "outputs" not in checkpoint_path.parts:
        raise ValueError("checkpoint must be an existing run artifact")

    config = RunConfig(
        mode="virtual",
        data_root=Path(r"E:\battery\data"),
        calibration_parameters_path=args.calibration_params,
    ).normalized(workspace)
    calibration = load_calibration_parameters(config.calibration_parameters_path)
    with checkpoint_path.open("rb") as handle:
        checkpoint = pickle.load(handle)
    state = checkpoint.state
    report: dict[str, object] = {
        "diagnostic_only": True,
        "source_checkpoint": str(checkpoint_path),
        "source_schema_version": checkpoint.schema_version,
        "source_cycle": checkpoint.aging_cycle,
        "source_state_hash": state.state_hash,
        "legacy": (
            {"status": "SKIPPED"}
            if args.skip_legacy
            else _legacy_attempt(config, calibration, state)
        ),
    }
    gc.collect()
    try:
        if (
            checkpoint.q_ref_ah is None
            or checkpoint.q_ref_node is None
            or checkpoint.initial_capacity_ah is None
        ):
            raise ValueError("checkpoint is missing charge analysis capacity references")
        report["continuous"] = _continuous_attempt(
            config,
            calibration,
            state,
            q_ref_ah=float(checkpoint.q_ref_ah),
            q_ref_node=int(checkpoint.q_ref_node),
            q_ref_initial_ah=float(checkpoint.initial_capacity_ah),
        )
    except Exception as exc:
        report["continuous"] = {
            "status": "FAILED",
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    write_json(output_path, report)
    return 0 if report["continuous"]["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
