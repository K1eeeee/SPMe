"""Run the user-authorized, non-formal cycle 0--122 capacity validation."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.config import RunConfig
from pybamm_w10.output import write_json
from pybamm_w10.runner import W10Runner, ensure_required_interpreter
from pybamm_w10.types import RunStatus


CYCLE122_RPT_NODES = (0, 25, 75, 122)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def cycle122_validation_config(
    workspace: Path, data_root: Path, calibration_path: Path
) -> RunConfig:
    """Build the fixed, non-formal cycle-122 validation configuration."""
    base = RunConfig(
        mode="virtual",
        data_root=data_root,
        calibration_parameters_path=calibration_path,
    )
    protocol = replace(
        base.protocol,
        max_aging_cycles=122,
        rpt_nodes=CYCLE122_RPT_NODES,
    )
    config = replace(base, protocol=protocol).normalized(workspace)
    if config.protocol.max_aging_cycles != 122 or config.protocol.rpt_nodes != CYCLE122_RPT_NODES:
        raise AssertionError("cycle-122 validation configuration was not preserved")
    return config


def validate_cycle122_resume_target(
    output_dir: Path,
    checkpoint_path: Path,
    config: RunConfig,
) -> Path:
    """Accept only a checkpoint owned by the same fixed cycle-122 run."""
    run_dir = output_dir.resolve()
    checkpoint = checkpoint_path.resolve()
    expected_checkpoint_dir = (run_dir / "checkpoints").resolve()
    if not checkpoint.is_file() or checkpoint.parent != expected_checkpoint_dir:
        raise ValueError("resume checkpoint must belong to the same validation run")

    scope_path = run_dir / "cycle122_validation_scope.json"
    if not scope_path.is_file():
        raise ValueError("cycle-122 resume requires the original validation scope audit")
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    expected = {
        "kind": "cycle_0_122_capacity_validation",
        "starts_from_cycle": 0,
        "max_aging_cycles": 122,
        "rpt_nodes": list(CYCLE122_RPT_NODES),
        "config_fingerprint": config.fingerprint(),
    }
    if any(scope.get(key) != value for key, value in expected.items()):
        raise ValueError("cycle-122 resume scope does not match the fixed validation configuration")
    return checkpoint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--calibration-params", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume-checkpoint", type=Path)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    output_dir = args.output_dir.resolve()
    calibration_path = args.calibration_params.resolve()
    resume_checkpoint = None if args.resume_checkpoint is None else args.resume_checkpoint.resolve()
    if not _inside(output_dir, workspace):
        raise ValueError("output directory must remain inside the isolated workspace")
    if resume_checkpoint is None and output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty validation output: {output_dir}")
    if not _inside(calibration_path, workspace):
        raise ValueError("calibration parameters must remain inside the isolated workspace")

    config = cycle122_validation_config(workspace, args.data_root, calibration_path)
    ensure_required_interpreter(config)
    if resume_checkpoint is not None:
        resume_checkpoint = validate_cycle122_resume_target(
            output_dir, resume_checkpoint, config
        )

    calibration = load_calibration_parameters(config.calibration_parameters_path)
    if calibration.calibration_status not in {"CAPACITY_CALIBRATED", "TRANSFERRED_FROM_DFN"}:
        raise ValueError("cycle-122 validation requires capacity-calibrated or transferred parameters")
    if calibration.degradation_parameter_status != "not_calibrated":
        raise ValueError("cycle-122 validation must preserve the existing uncalibrated degradation scales")
    if any(
        value != 1.0
        for name, value in calibration.values.items()
        if name != "capacity_scale_factor"
    ):
        raise ValueError("cycle-122 validation must preserve unit degradation scale factors")

    if resume_checkpoint is None:
        write_json(
            output_dir / "cycle122_validation_scope.json",
            {
                "kind": "cycle_0_122_capacity_validation",
                "diagnostic_only": True,
                "formal_350_cycle_run": False,
                "starts_from_cycle": 0,
                "max_aging_cycles": 122,
                "rpt_nodes": list(CYCLE122_RPT_NODES),
                "udds_strict_numerical_certification": "waived_by_user",
                "charge_strict_numerical_certification": "waived_by_user",
                "charge_solver_values": "legacy global values: rtol=1e-5, atol=1e-7, dt_max=1.0 s",
                "protocol": asdict(config.protocol),
                "solver": asdict(config.solver),
                "protocol_algorithm_version": config.protocol_algorithm_version,
                "solver_execution_version": config.solver_execution_version,
                "checkpoint_schema_version": config.checkpoint_schema_version,
                "output_schema_version": config.output_schema_version,
                "config_fingerprint": config.fingerprint(),
            },
        )
    status = W10Runner(config, workspace, calibration).run(
        output_dir,
        resume_checkpoint=resume_checkpoint,
    )
    return 0 if status == RunStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
