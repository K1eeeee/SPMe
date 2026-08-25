"""Compare overlapping cycle summaries using the approved strict regression limits."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pybamm_w10.output import write_json


LIMITS = {
    "terminal_voltage_v": 1e-6,
    "temperature_k": 1e-4,
    "charge_ah": 1e-6,
    "discharge_ah": 1e-6,
}


def _rows(path: Path) -> dict[int, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {int(row["cycle"]): row for row in csv.DictReader(handle)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-cycle", type=int, default=9)
    args = parser.parse_args()

    old_rows = _rows(args.old.resolve())
    new_rows = _rows(args.new.resolve())
    cycles = sorted(set(old_rows) & set(new_rows) & set(range(1, args.max_cycle + 1)))
    if not cycles:
        raise ValueError("no overlapping regression cycles")

    details: list[dict[str, object]] = []
    passed = True
    for cycle in cycles:
        old = old_rows[cycle]
        new = new_rows[cycle]
        differences = {
            field: abs(float(new[field]) - float(old[field]))
            for field in LIMITS
        }
        termination_equal = (
            new["termination_classification"] == old["termination_classification"]
        )
        cycle_passed = termination_equal and all(
            differences[field] <= limit for field, limit in LIMITS.items()
        )
        passed = passed and cycle_passed
        details.append({
            "cycle": cycle,
            "passed": cycle_passed,
            "differences": differences,
            "termination_equal": termination_equal,
        })

    write_json(args.output.resolve(), {
        "status": "PASSED" if passed else "FAILED",
        "limits": LIMITS,
        "compared_cycles": cycles,
        "details": details,
        "old_summary": str(args.old.resolve()),
        "new_summary": str(args.new.resolve()),
    })
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
