"""Run the bounded cycle 0-25 solver-resilience regression in a new directory."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pybamm_w10.calibration.parameters import load_calibration_parameters
from pybamm_w10.config import RunConfig
from pybamm_w10.output import write_json
from pybamm_w10.runner import W10Runner, ensure_required_interpreter
from pybamm_w10.types import RunStatus


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--calibration-params", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--max-aging-cycles",
        type=int,
        choices=(1, 25),
        default=25,
        help="use 1 for the pre-gate comparison or 25 for the full bounded regression",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    output_dir = args.output_dir.resolve()
    calibration_path = args.calibration_params.resolve()
    if not _inside(output_dir, workspace):
        raise ValueError("output directory must remain inside the isolated workspace")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty regression output: {output_dir}")
    if not _inside(calibration_path, workspace):
        raise ValueError("calibration parameters must remain inside the isolated workspace")

    base = RunConfig(
        mode="virtual",
        data_root=args.data_root,
        calibration_parameters_path=calibration_path,
    )
    rpt_nodes = (0,) if args.max_aging_cycles == 1 else (0, 25)
    protocol = replace(
        base.protocol,
        max_aging_cycles=args.max_aging_cycles,
        rpt_nodes=rpt_nodes,
    )
    config = replace(base, protocol=protocol).normalized(workspace)
    if (
        config.protocol.max_aging_cycles != args.max_aging_cycles
        or config.protocol.rpt_nodes != rpt_nodes
    ):
        raise AssertionError("bounded regression configuration was not preserved")
    ensure_required_interpreter(config)

    calibration = load_calibration_parameters(config.calibration_parameters_path)
    if calibration.calibration_status not in {"CAPACITY_CALIBRATED", "TRANSFERRED_FROM_DFN"}:
        raise ValueError("regression requires capacity-calibrated or transferred parameters")
    if calibration.degradation_parameter_status != "not_calibrated":
        raise ValueError("regression must preserve the existing uncalibrated degradation scales")
    if any(
        value != 1.0
        for name, value in calibration.values.items()
        if name != "capacity_scale_factor"
    ):
        raise ValueError("regression must preserve unit degradation scale factors")

    write_json(
        output_dir / "regression_scope.json",
        {
            "kind": "cycle_0_25_solver_resilience_regression",
            "diagnostic_only": True,
            "formal_350_cycle_run": False,
            "starts_from_cycle": 0,
            "max_aging_cycles": args.max_aging_cycles,
            "rpt_nodes": list(rpt_nodes),
            "protocol": asdict(config.protocol),
            "solver": asdict(config.solver),
            "protocol_algorithm_version": config.protocol_algorithm_version,
            "solver_execution_version": config.solver_execution_version,
            "checkpoint_schema_version": config.checkpoint_schema_version,
            "output_schema_version": config.output_schema_version,
            "config_fingerprint": config.fingerprint(),
        },
    )
    status = W10Runner(config, workspace, calibration).run(output_dir)
    return 0 if status == RunStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
