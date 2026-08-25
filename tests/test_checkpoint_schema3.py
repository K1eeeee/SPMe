from __future__ import annotations

import pytest
from dataclasses import replace

from pybamm_w10.config import RunConfig
from pybamm_w10.output import build_output_manifest, load_checkpoint, save_checkpoint
from pybamm_w10.types import Checkpoint, NumericalFailure, ProtocolPhase
from pybamm_w10.udds import CurrentProfile
import numpy as np


def _checkpoint(config: RunConfig, manifest, *, schema: int = 6) -> Checkpoint:
    return Checkpoint(
        schema_version=schema, state=None, aging_cycle=0, main_time_s=0.0, mode="virtual",
        q_ref_ah=None, q_ref_node=None, initial_capacity_ah=None,
        protocol_phase=ProtocolPhase.INITIAL_RPT, capacity_targets=None,
        config_fingerprint=config.fingerprint(), input_fingerprint="input", udds_fingerprint="udds",
        initial_state_fingerprint="initial", environment_fingerprint="environment",
        result_transaction=0, output_manifest=manifest,
        protocol_algorithm_version=config.protocol_algorithm_version,
        output_schema_version=config.output_schema_version,
        guard_config_fingerprint=config.guard_fingerprint(),
        last_successful_boundary="cycle-000", last_successful_stage=ProtocolPhase.INITIAL_RPT,
        effective_parameters_fingerprint="",
        charge_efficiency_algorithm_version=config.charge_efficiency_algorithm_version,
        solver_execution_version=config.solver_execution_version,
    )


def test_schema6_checkpoint_requires_all_new_fingerprints(workspace_tmp) -> None:
    config = RunConfig()
    manifest = build_output_manifest(workspace_tmp, 0, 0, None)
    path = workspace_tmp / "checkpoint.pkl"
    save_checkpoint(path, _checkpoint(config, manifest))
    loaded = load_checkpoint(path, config, "udds", input_fingerprint="input", initial_state_fingerprint="initial", environment_fingerprint="environment")
    assert loaded.schema_version == 6
    bad_path = workspace_tmp / "bad-guard.pkl"
    save_checkpoint(bad_path, replace(_checkpoint(config, manifest), guard_config_fingerprint="wrong"))
    with pytest.raises(NumericalFailure, match="guard"):
        load_checkpoint(bad_path, config, "udds", input_fingerprint="input", initial_state_fingerprint="initial", environment_fingerprint="environment")

    effective_path = workspace_tmp / "effective.pkl"
    save_checkpoint(effective_path, replace(_checkpoint(config, manifest), effective_parameters_fingerprint="audit-a"))
    with pytest.raises(NumericalFailure, match="effective parameters"):
        load_checkpoint(
            effective_path,
            config,
            "udds",
            input_fingerprint="input",
            initial_state_fingerprint="initial",
            environment_fingerprint="environment",
            effective_parameters_fingerprint="audit-b",
        )
    solver_path = workspace_tmp / "solver.pkl"
    save_checkpoint(solver_path, replace(_checkpoint(config, manifest), solver_execution_version="old"))
    with pytest.raises(NumericalFailure, match="solver execution"):
        load_checkpoint(solver_path, config, "udds")


def test_schema5_and_failure_snapshots_are_explicitly_rejected(workspace_tmp) -> None:
    config = RunConfig()
    manifest = build_output_manifest(workspace_tmp, 0, 0, None)
    old = workspace_tmp / "old.pkl"
    save_checkpoint(old, _checkpoint(config, manifest, schema=5))
    with pytest.raises(NumericalFailure, match="UNSUPPORTED_CHECKPOINT_SCHEMA"):
        load_checkpoint(old, config, "udds")
