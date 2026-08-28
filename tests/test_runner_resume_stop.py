from __future__ import annotations

from dataclasses import replace
import csv

import pytest

from pybamm_w10.config import RunConfig
from pybamm_w10.output import build_output_manifest, write_output_manifest
from pybamm_w10.charge_variables import ResolvedChargeVariables
from pybamm_w10.model import effective_parameters_fingerprint
from pybamm_w10.runner import (
    checkpoint_reaches_stop,
    validate_current_charge_inventory,
    validate_current_effective_parameters,
    validate_selected_output_manifest,
)
from pybamm_w10.types import Checkpoint, NumericalFailure, ProtocolPhase


def _checkpoint(config: RunConfig, manifest, *, phase=ProtocolPhase.POST_RPT_RECOVERY) -> Checkpoint:
    return Checkpoint(
        schema_version=config.checkpoint_schema_version,
        state=None,
        aging_cycle=25,
        main_time_s=0.0,
        mode=config.mode,
        q_ref_ah=4.8,
        q_ref_node=25,
        initial_capacity_ah=4.9,
        protocol_phase=phase,
        capacity_targets=None,
        config_fingerprint=config.fingerprint(),
        input_fingerprint="input",
        udds_fingerprint="udds",
        initial_state_fingerprint="initial",
        environment_fingerprint="environment",
        result_transaction=1,
        output_manifest=manifest,
    )


def _committed_rpt(workspace_tmp):
    run_dir = workspace_tmp / "run"
    run_dir.mkdir()
    with (run_dir / "rpt_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("node", "capacity_ah"))
        writer.writeheader()
        writer.writerow({"node": 0, "capacity_ah": 4.9})
        writer.writerow({"node": 25, "capacity_ah": 4.8})
    return run_dir, build_output_manifest(run_dir, 1, 25, 25)


def test_validated_complete_rpt_checkpoint_reaches_stop_without_next_cycle(workspace_tmp) -> None:
    run_dir, manifest = _committed_rpt(workspace_tmp)
    checkpoint = _checkpoint(RunConfig(), manifest)

    assert checkpoint_reaches_stop(run_dir, checkpoint, 25, (0, 25, 75)) is True
    assert checkpoint_reaches_stop(
        run_dir,
        replace(checkpoint, protocol_phase=ProtocolPhase.RPT_PRECONDITIONING),
        25,
        (0, 25, 75),
    ) is False
    assert checkpoint_reaches_stop(
        run_dir,
        replace(checkpoint, output_manifest=replace(manifest, last_rpt_node=0)),
        25,
        (0, 25, 75),
    ) is False


def test_public_output_manifest_must_select_matching_checkpoint(workspace_tmp) -> None:
    run_dir, manifest = _committed_rpt(workspace_tmp)
    checkpoint = _checkpoint(RunConfig(), manifest)
    path = run_dir / "checkpoints" / "cycle-025.pkl"
    path.parent.mkdir()
    path.write_bytes(b"selected")
    write_output_manifest(run_dir / "output_manifest.json", manifest, checkpoint=path.name)
    validate_selected_output_manifest(run_dir, path, checkpoint)

    write_output_manifest(run_dir / "output_manifest.json", manifest, checkpoint="cycle-024.pkl")
    with pytest.raises(NumericalFailure, match="does not select"):
        validate_selected_output_manifest(run_dir, path, checkpoint)

    write_output_manifest(run_dir / "output_manifest.json", replace(manifest, transaction=2), checkpoint=path.name)
    with pytest.raises(NumericalFailure, match="commit"):
        validate_selected_output_manifest(run_dir, path, checkpoint)


def test_current_effective_parameters_are_compared_with_committed_audit() -> None:
    saved = {"parameters": {"sei": 1.0}}
    saved["fingerprint"] = effective_parameters_fingerprint(saved)
    current = dict(saved)
    validate_current_effective_parameters(saved, current)

    changed = {"parameters": {"sei": 3.16}}
    changed["fingerprint"] = effective_parameters_fingerprint(changed)
    with pytest.raises(NumericalFailure, match="current effective parameters"):
        validate_current_effective_parameters(saved, changed)


def test_current_charge_inventory_is_compared_with_committed_inventory() -> None:
    inventory = ResolvedChargeVariables("model", "version", "options", (), True, True)
    saved = inventory.to_json()
    import hashlib
    import json

    saved["inventory_sha256"] = hashlib.sha256(
        json.dumps(saved, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    validate_current_charge_inventory(saved, inventory)

    changed = ResolvedChargeVariables("changed", "version", "options", (), True, True)
    with pytest.raises(NumericalFailure, match="current charge-efficiency"):
        validate_current_charge_inventory(saved, changed)
