"""Explicit command-line entry point; dry run is deliberately the default."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from .config import RunConfig
from .calibration.parameters import (
    CalibrationParameterError,
    load_calibration_parameters,
    require_formal_run_ready,
)
from .calibration.capacity import CapacityCalibrationError, run_capacity_calibration
from .calibration.aging import AgingCalibrationError, STAGE1_DIRNAME, Stage1AgingCalibration
from .backend import construct_initial_state_record
from .model import build_spme, environment_metadata
from .output import RunDirectoryBusy, write_json
from .runner import W10Runner, ensure_required_interpreter
from .types import RunStatus


def _isolated_workspace(path: Path) -> Path:
    expected = Path(__file__).resolve().parents[2]
    workspace = path.resolve()
    if workspace != expected:
        raise ValueError(f"workspace must be the isolated project: {expected}")
    return workspace


def _ensure_within_workspace(path: Path, workspace: Path, label: str) -> None:
    resolved = path.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"{label} must be within the isolated workspace: {workspace}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Construct or explicitly run the W10 PyBaMM SPMe aging model")
    parser.add_argument("--mode", choices=("virtual", "strict-w10"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, required=True, help="read-only root containing LG M50T data")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--prepare", action="store_true", help="build SPMe and process W10 waveform, without solving")
    actions.add_argument("--run", action="store_true", help="execute the 350-cycle aging simulation")
    actions.add_argument("--resume", type=Path, help="resume from a committed checkpoint")
    actions.add_argument("--smoke", action="store_true", help="run the short real-PyBaMM readiness smoke test")
    actions.add_argument("--charge-efficiency-smoke", action="store_true", help="run only the real four-stage charge-efficiency smoke test")
    actions.add_argument("--calibrate-capacity", action="store_true", help="run isolated strict-W10 cycle-0 capacity calibration")
    actions.add_argument("--calibrate-soh-stage1", action="store_true", help="run resumable W10 stage-1 SOH aging calibration")
    actions.add_argument(
        "--evaluate-soh",
        type=Path,
        metavar="RUN_DIR",
        help="evaluate a completed run against all W10 capacity diagnostics",
    )
    actions.add_argument(
        "--verify-repaired-aging",
        action="store_true",
        help="run the user-authorized 350-cycle virtual verification with the repaired Step 6 protocol",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--calibration-params", type=Path, help="versioned calibrated-parameter JSON")
    args = parser.parse_args(argv)
    try:
        workspace = _isolated_workspace(args.workspace)
        if args.output_dir is not None:
            _ensure_within_workspace(args.output_dir, workspace, "output directory")
        if args.resume is not None:
            _ensure_within_workspace(args.resume, workspace, "resume checkpoint")
        if args.evaluate_soh is not None:
            _ensure_within_workspace(args.evaluate_soh, workspace, "SOH evaluation run directory")
        if args.calibration_params is not None:
            _ensure_within_workspace(args.calibration_params, workspace, "calibration parameters")
        mode = args.mode or ("strict-w10" if args.calibrate_capacity else "virtual")
        config = RunConfig(
            mode=mode,
            data_root=args.data_root,
            calibration_parameters_path=args.calibration_params,
        ).normalized(workspace)
        calibration_parameters = (
            None
            if config.calibration_parameters_path is None
            else load_calibration_parameters(config.calibration_parameters_path)
        )
    except (CalibrationParameterError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    if args.evaluate_soh is not None:
        from .evaluation import SohEvaluationError, evaluate_soh_comparison

        try:
            metrics = evaluate_soh_comparison(args.evaluate_soh, config.data_root)
        except (OSError, SohEvaluationError, ValueError) as exc:
            print(f"SOH EVALUATION FAILED: {exc}")
            return 2
        print({"status": "SOH_EVALUATED", **asdict(metrics)})
        return 0

    runner = W10Runner(config, workspace, calibration_parameters)
    if args.calibrate_soh_stage1:
        if calibration_parameters is None:
            print("ERROR: --calibrate-soh-stage1 requires --calibration-params")
            return 2
        if (
            calibration_parameters.calibration_status not in {"CAPACITY_CALIBRATED", "TRANSFERRED_FROM_DFN"}
            or calibration_parameters.degradation_parameter_status != "not_calibrated"
            or calibration_parameters.capacity_scale_factor != 0.95630859375
        ):
            print("ERROR: stage-1 requires capacity_scale_factor=0.95630859375 and uncalibrated degradation scales")
            return 2
        output_dir = args.output_dir or workspace / "outputs" / "pybamm_spme_calibration" / STAGE1_DIRNAME
        try:
            result = Stage1AgingCalibration(config, workspace, output_dir, calibration_parameters).run()
        except (AgingCalibrationError, RunDirectoryBusy, ValueError, RuntimeError) as exc:
            print(f"STAGE-1 CALIBRATION FAILED: {exc}")
            return 2
        print({key: result[key] for key in ("status", "winner", "validated_candidate", "backup", "calibration", "holdout") if key in result})
        return 0 if result.get("status") == "COMPLETED" else 1
    if args.run or args.resume:
        if calibration_parameters is None:
            print("ERROR: formal run requires --calibration-params with PARAMETERS_FROZEN status")
            return 2
        try:
            require_formal_run_ready(calibration_parameters)
            status = runner.run(args.output_dir, resume_checkpoint=args.resume)
        except (CalibrationParameterError, RunDirectoryBusy, ValueError, RuntimeError) as exc:
            print(f"ERROR: {exc}")
            return 2
        return 0 if status == RunStatus.COMPLETED else 1
    if args.calibrate_capacity:
        if args.mode == "virtual":
            print("ERROR: --calibrate-capacity fixes mode to strict-w10; --mode virtual is not allowed")
            return 2
        output_dir = args.output_dir or workspace / "outputs" / "pybamm_spme_calibration" / "m50t-w10-v1"
        try:
            ensure_required_interpreter(config)
            result = run_capacity_calibration(config, output_dir)
        except (CapacityCalibrationError, RunDirectoryBusy, ValueError, RuntimeError) as exc:
            print(f"CAPACITY CALIBRATION FAILED: {exc}")
            return 2
        print({
            "status": "CAPACITY_CALIBRATED",
            "scale_factor": result.winner.scale_factor,
            "capacity_ah": result.winner.capacity_ah,
            "relative_error": result.winner.relative_error,
            "repeat_relative_difference": result.repeat_relative_difference,
        })
        return 0
    if args.verify_repaired_aging:
        if args.mode == "strict-w10":
            print("ERROR: --verify-repaired-aging preserves the previous virtual-mode configuration")
            return 2
        if calibration_parameters is None:
            print("ERROR: --verify-repaired-aging requires --calibration-params from cycle-0 capacity calibration")
            return 2
        if (
            calibration_parameters.calibration_status
            not in {"CAPACITY_CALIBRATED", "TRANSFERRED_FROM_DFN"}
            or calibration_parameters.degradation_parameter_status != "not_calibrated"
            or any(value != 1.0 for name, value in calibration_parameters.values.items() if name != "capacity_scale_factor")
        ):
            print("ERROR: verification requires CAPACITY_CALIBRATED parameters with uncalibrated unit degradation scales")
            return 2
        output_dir = args.output_dir or workspace / "outputs" / "pybamm_spme" / "aging-350-verification-v1"
        if output_dir.exists() and any(output_dir.iterdir()):
            print(f"ERROR: refusing to overwrite existing verification output: {output_dir}")
            return 2
        try:
            write_json(
                output_dir / "verification_authorization.json",
                {
                    "kind": "user_authorized_repaired_aging_verification",
                    "model": "SPMe",
                    "purpose": "verify Step 6 capacity-event and drive-cycle guard repair over 350 aging cycles",
                    "mode": config.mode,
                    "max_aging_cycles": config.protocol.max_aging_cycles,
                    "calibration_parameters_used": True,
                    "calibration_parameters_path": str(config.calibration_parameters_path),
                    "calibration_source_model": calibration_parameters.source_model,
                    "capacity_scale_factor": calibration_parameters.capacity_scale_factor,
                    "degradation_parameter_status": calibration_parameters.degradation_parameter_status,
                    "protocol_algorithm_version": config.protocol_algorithm_version,
                    "config_fingerprint": config.fingerprint(),
                    "formal_calibrated_prediction": False,
                },
            )
            status = runner.run(output_dir)
        except (RunDirectoryBusy, ValueError, RuntimeError) as exc:
            print(f"REPAIRED AGING VERIFICATION FAILED: {exc}")
            return 2
        return 0 if status == RunStatus.COMPLETED else 1
    if args.smoke:
        from .smoke import run_smoke

        try:
            ensure_required_interpreter(config)
            run_smoke(config, args.output_dir)
        except (RunDirectoryBusy, ValueError, RuntimeError) as exc:
            print(f"SMOKE FAILED: {exc}")
            return 2
        return 0
    if args.charge_efficiency_smoke:
        from .smoke import run_charge_efficiency_smoke

        try:
            ensure_required_interpreter(config)
            run_charge_efficiency_smoke(config, args.output_dir)
        except (RunDirectoryBusy, ValueError, RuntimeError) as exc:
            print(f"CHARGE-EFFICIENCY SMOKE FAILED: {exc}")
            return 2
        return 0
    if args.prepare:
        ensure_required_interpreter(config)
        profile, validation = runner.prepare_profile()
        artifacts = build_spme(config)
        initial = construct_initial_state_record(artifacts, config)
        print({
            "profile_points": len(profile.time_s),
            "period_s": validation["period_identification"]["selected_period_s"],
            "step14_segments": len(validation["period_identification"]["segment_best_periods_s"]),
            "complete_units": validation["complete_unit_count"],
            "profile_fingerprint": validation["profile_fingerprint"],
            "initial_state_fingerprint": initial.fingerprint,
            "environment": environment_metadata(artifacts),
        })
    else:
        print("Dry run only: pass --prepare, --smoke, --run, or --resume explicitly.")
    return 0
