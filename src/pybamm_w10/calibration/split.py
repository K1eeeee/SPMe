"""Fixed W10 split views that prevent ordinary calibration code reading holdout targets."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .data import W10_DIAGNOSTIC_NODES, _read_capacity_endpoint
from .artifacts import write_calibration_json


CALIBRATION_NODES = W10_DIAGNOSTIC_NODES[:10]
HOLDOUT_NODES = W10_DIAGNOSTIC_NODES[10:]


class HoldoutAccessDenied(PermissionError):
    """The frozen-parameter gate has not been satisfied."""


def calibration_split_metadata() -> dict[str, object]:
    return {
        "calibration_nodes": list(CALIBRATION_NODES),
        "holdout_nodes": list(HOLDOUT_NODES),
        "holdout_accessed": False,
    }


def _capacity_path(data_root: Path, diagnostic_index: int) -> Path:
    return (
        data_root.resolve()
        / "LG M50T"
        / "_processed_mat"
        / f"W10_capacity_diagnostic_{diagnostic_index:02d}.csv"
    )


def _targets_for_nodes(data_root: Path, nodes: tuple[int, ...]) -> dict[int, float]:
    node_to_index = {node: index for index, node in enumerate(W10_DIAGNOSTIC_NODES, start=1)}
    return {node: _read_capacity_endpoint(_capacity_path(data_root, node_to_index[node])) for node in nodes}


def load_calibration_capacity_targets(data_root: Path) -> dict[int, float]:
    """Return only cycle 0–225 targets; callers cannot request holdout nodes."""
    return _targets_for_nodes(data_root, CALIBRATION_NODES)


def load_holdout_capacity_targets(
    data_root: Path,
    *,
    parameter_status: str,
    frozen_parameters_hash: str,
    audit_path: Path,
) -> dict[int, float]:
    """Read held-out targets only after parameters are frozen, and audit access.

    The frozen parameter file itself is not opened or modified, so a holdout
    pass/fail cannot overwrite its content hash.
    """
    if parameter_status != "PARAMETERS_FROZEN":
        raise HoldoutAccessDenied("holdout targets require PARAMETERS_FROZEN")
    if len(frozen_parameters_hash) != 64:
        raise HoldoutAccessDenied("frozen parameter hash must be a SHA-256 digest")
    int(frozen_parameters_hash, 16)
    targets = _targets_for_nodes(data_root, HOLDOUT_NODES)
    audit: dict[str, Any] = {
        "accessed_at_utc": datetime.now(timezone.utc).isoformat(),
        "parameter_status": parameter_status,
        "frozen_parameters_hash": frozen_parameters_hash,
        "holdout_nodes": list(HOLDOUT_NODES),
        "target_payload_sha256": sha256(repr(sorted(targets.items())).encode("utf-8")).hexdigest(),
    }
    write_calibration_json(audit_path, audit)
    return targets
