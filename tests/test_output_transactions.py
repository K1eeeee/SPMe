from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from pybamm_w10.output import (
    RunDirectoryBusy,
    RunDirectoryLock,
    append_json_line,
    append_log,
    build_output_manifest,
    prepare_run_directory,
    rollback_to_checkpoint,
    write_json,
)
from pybamm_w10.types import Checkpoint, NumericalFailure, ProtocolPhase


def _hold_lock(path: str, ready, release) -> None:
    with RunDirectoryLock(Path(path), {"mode": "holder"}):
        ready.set()
        release.wait(10)


def _checkpoint(manifest) -> Checkpoint:
    return Checkpoint(
        schema_version=3,
        state=None,
        aging_cycle=5,
        main_time_s=12.0,
        mode="virtual",
        q_ref_ah=4.0,
        q_ref_node=0,
        initial_capacity_ah=4.0,
        protocol_phase=ProtocolPhase.CYCLE_COMPLETED,
        capacity_targets=None,
        config_fingerprint="config",
        input_fingerprint="input",
        udds_fingerprint="udds",
        initial_state_fingerprint="initial",
        environment_fingerprint="environment",
        result_transaction=3,
        output_manifest=manifest,
    )


def test_uncommitted_tails_and_artifacts_are_archived_then_rollback_is_idempotent(workspace_tmp) -> None:
    run_dir = prepare_run_directory(workspace_tmp / "run")
    append_log(run_dir / "run.log", "committed")
    append_json_line(run_dir / "udds_cycle_validation.jsonl", {"cycle": 1})
    write_json(run_dir / "run_config.json", {"mode": "virtual"})
    (run_dir / "timeseries" / "cycle-001.csv").write_text("time,current\n0,0\n", encoding="utf-8")
    manifest = build_output_manifest(run_dir, 3, 5, 0)
    checkpoint = _checkpoint(manifest)
    selected = run_dir / "checkpoints" / "cycle-005.pkl"
    selected.write_bytes(b"selected")

    append_log(run_dir / "run.log", "uncommitted")
    append_json_line(run_dir / "udds_cycle_validation.jsonl", {"cycle": 2})
    (run_dir / "timeseries" / "cycle-006.csv").write_text("uncommitted", encoding="utf-8")
    (run_dir / "figures" / "future.png").write_bytes(b"future")
    (run_dir / "effective_parameters.json").write_text("{}", encoding="utf-8")
    (run_dir / "failures" / "failure-test.json").write_text("{}", encoding="utf-8")
    (run_dir / "failures" / "failure-test.pkl").write_bytes(b"forensic")
    (run_dir / "checkpoints" / "cycle-010.pkl").write_bytes(b"future")

    audit = rollback_to_checkpoint(run_dir, selected, checkpoint)
    assert (run_dir / "run.log").read_text(encoding="utf-8") == "committed\n"
    assert '"cycle": 2' not in (run_dir / "udds_cycle_validation.jsonl").read_text(encoding="utf-8")
    assert not (run_dir / "timeseries" / "cycle-006.csv").exists()
    assert not (run_dir / "figures" / "future.png").exists()
    assert not (run_dir / "effective_parameters.json").exists()
    assert not (run_dir / "failures" / "failure-test.json").exists()
    assert not (run_dir / "failures" / "failure-test.pkl").exists()
    assert not (run_dir / "checkpoints" / "cycle-010.pkl").exists()
    assert audit["rollback_archive"]
    assert list((run_dir / audit["rollback_archive"]).rglob("*"))

    second = rollback_to_checkpoint(run_dir, selected, checkpoint)
    assert second["truncated_bytes"] == {}
    assert second["moved_files"] == []


def test_committed_prefix_modification_is_rejected_not_truncated(workspace_tmp) -> None:
    run_dir = prepare_run_directory(workspace_tmp / "run")
    append_log(run_dir / "run.log", "committed")
    manifest = build_output_manifest(run_dir, 1, 0, 0)
    checkpoint = _checkpoint(manifest)
    selected = run_dir / "checkpoints" / "cycle-005.pkl"
    selected.write_bytes(b"selected")
    (run_dir / "run.log").write_text("corrupted\n", encoding="utf-8")
    with pytest.raises(NumericalFailure, match="prefix"):
        rollback_to_checkpoint(run_dir, selected, checkpoint)
    assert (run_dir / "run.log").read_text(encoding="utf-8") == "corrupted\n"


def test_interrupted_rollback_can_be_retried_to_completion(workspace_tmp) -> None:
    run_dir = prepare_run_directory(workspace_tmp / "interrupted")
    append_log(run_dir / "run.log", "committed")
    manifest = build_output_manifest(run_dir, 1, 5, 0)
    checkpoint = _checkpoint(manifest)
    selected = run_dir / "checkpoints" / "cycle-005.pkl"
    selected.write_bytes(b"selected")
    append_log(run_dir / "run.log", "tail")
    (run_dir / "figures" / "tail.png").write_bytes(b"tail")
    with pytest.raises(RuntimeError, match="injected"):
        rollback_to_checkpoint(
            run_dir, selected, checkpoint, _failure_after_actions=1
        )
    audit = rollback_to_checkpoint(run_dir, selected, checkpoint)
    assert (run_dir / "run.log").read_text(encoding="utf-8") == "committed\n"
    assert not (run_dir / "figures" / "tail.png").exists()
    assert "figures/tail.png" in audit["moved_files"]


def test_exclusive_lock_blocks_a_second_process_before_output_access(workspace_tmp) -> None:
    run_dir = workspace_tmp / "contended"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_lock, args=(str(run_dir), ready, release))
    process.start()
    assert ready.wait(10)
    try:
        with pytest.raises(RunDirectoryBusy):
            with RunDirectoryLock(run_dir, {"mode": "competitor"}):
                pass
        assert not (run_dir / "cycle_summary.csv").exists()
        assert not (run_dir / "rpt_summary.csv").exists()
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0
