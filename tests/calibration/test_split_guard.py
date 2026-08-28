from __future__ import annotations

from hashlib import sha256

import pytest

from pybamm_w10.calibration.split import (
    ANCHOR_NODES,
    CALIBRATION_NODES,
    HOLDOUT_NODES,
    HoldoutAccessDenied,
    calibration_split_metadata,
    load_calibration_capacity_targets,
    load_holdout_capacity_targets,
)

from .test_data_inventory import make_w10_data_root


def test_calibration_view_never_returns_holdout_targets(workspace_tmp) -> None:
    root = make_w10_data_root(workspace_tmp / "data")
    targets = load_calibration_capacity_targets(root)

    assert tuple(targets) == ANCHOR_NODES + CALIBRATION_NODES
    assert not set(targets) & set(HOLDOUT_NODES)
    assert calibration_split_metadata()["holdout_accessed"] is False


def test_holdout_requires_frozen_parameters_and_keeps_parameter_file_unchanged(workspace_tmp) -> None:
    root = make_w10_data_root(workspace_tmp / "data")
    parameter_path = workspace_tmp / "frozen_parameters.json"
    parameter_path.write_text('{"status":"PARAMETERS_FROZEN"}', encoding="utf-8")
    original = parameter_path.read_bytes()
    digest = sha256(original).hexdigest()
    audit_path = workspace_tmp / "holdout_access.json"

    with pytest.raises(HoldoutAccessDenied, match="PARAMETERS_FROZEN"):
        load_holdout_capacity_targets(
            root,
            parameter_status="CAPACITY_CALIBRATED",
            frozen_parameters_hash=digest,
            audit_path=audit_path,
        )
    assert not audit_path.exists()

    targets = load_holdout_capacity_targets(
        root,
        parameter_status="PARAMETERS_FROZEN",
        frozen_parameters_hash=digest,
        audit_path=audit_path,
        frozen_parameters_path=parameter_path,
    )
    assert tuple(targets) == HOLDOUT_NODES
    assert parameter_path.read_bytes() == original
    assert digest == sha256(parameter_path.read_bytes()).hexdigest()
    assert audit_path.is_file()

    with pytest.raises(HoldoutAccessDenied, match="hash"):
        load_holdout_capacity_targets(
            root,
            parameter_status="PARAMETERS_FROZEN",
            frozen_parameters_hash="0" * 64,
            audit_path=workspace_tmp / "bad_holdout_access.json",
            frozen_parameters_path=parameter_path,
        )
