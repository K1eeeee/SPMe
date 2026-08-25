"""Compare the completed cycle-122 RPT capacity with W10 node 122."""

from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pybamm_w10.calibration.data import _read_capacity_endpoint
from pybamm_w10.output import write_json


EXPECTED_RPT_NODES = {0, 25, 75, 122}


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _simulated_capacities(path: Path) -> dict[int, float]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        result = {int(row["node"]): float(row["capacity_ah"]) for row in rows}
    if set(result) != EXPECTED_RPT_NODES:
        raise ValueError(
            f"cycle-122 validation requires RPT nodes {sorted(EXPECTED_RPT_NODES)}; "
            f"got {sorted(result)}"
        )
    if any(value <= 0 for value in result.values()):
        raise ValueError("simulated RPT capacities must be positive")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    status_path = run_dir / "run_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read run status: {status_path}") from exc
    if status.get("status") != "COMPLETED":
        raise ValueError("capacity comparison requires a completed cycle-122 run")

    simulated_path = run_dir / "rpt_summary.csv"
    simulated = _simulated_capacities(simulated_path)
    experimental_dir = args.data_root.resolve() / "LG M50T" / "_processed_mat"
    experimental_0_path = experimental_dir / "W10_capacity_diagnostic_01.csv"
    experimental_122_path = experimental_dir / "W10_capacity_diagnostic_04.csv"
    experimental_0 = _read_capacity_endpoint(experimental_0_path)
    experimental_122 = _read_capacity_endpoint(experimental_122_path)

    simulated_soh = 100.0 * simulated[122] / simulated[0]
    experimental_soh = 100.0 * experimental_122 / experimental_0
    payload = {
        "evaluation_schema_version": 1,
        "cycle": 122,
        "normalization": "each_curve_cycle_0_capacity",
        "error_definition": "simulated_soh_pct - experimental_soh_pct",
        "error_unit": "percentage_points",
        "simulated_capacity_ah": simulated[122],
        "experimental_capacity_ah": experimental_122,
        "capacity_error_ah": simulated[122] - experimental_122,
        "simulated_soh_pct": simulated_soh,
        "experimental_soh_pct": experimental_soh,
        "soh_error_percentage_points": simulated_soh - experimental_soh,
        "absolute_soh_error_percentage_points": abs(simulated_soh - experimental_soh),
        "provenance": {
            "rpt_summary_sha256": _file_hash(simulated_path),
            "experimental_cycle_0_sha256": _file_hash(experimental_0_path),
            "experimental_cycle_122_sha256": _file_hash(experimental_122_path),
        },
    }
    output = run_dir / "cycle122_capacity_accuracy.json"
    write_json(output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
