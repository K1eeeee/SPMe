"""Atomic calibration artifact helpers shared by later calibration phases."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from ..output import _atomic_bytes, write_json


def write_calibration_json(path: Path, value: dict[str, Any]) -> Path:
    """Atomically write one calibration artifact without touching raw inputs."""
    write_json(path, value)
    return path


def write_calibration_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> Path:
    """Atomically persist a complete, small calibration table.

    Calibration search tables are rewritten as complete snapshots rather than
    appended, so an interrupted candidate cannot leave a partial row behind.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_bytes(path, buffer.getvalue().encode("utf-8"))
    return path
