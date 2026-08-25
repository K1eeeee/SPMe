"""Short real-PyBaMM readiness test; never runs an aging cycle."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np

from .backend import PyBaMMBackend, construct_initial_state_record
from .config import RunConfig
from .charge_efficiency import build_charge_analysis_bundle
from .charge_variables import preflight_charge_variables, write_charge_efficiency_variable_inventory
from .diagnostics import run_capacity_rpt
from .model import (
    build_spme,
    effective_parameters_audit,
    effective_parameters_fingerprint,
    environment_metadata,
)
from .output import (
    RunDirectoryLock,
    append_json_line,
    append_log,
    append_charge_efficiency_summary,
    append_charge_soc_bins,
    build_output_manifest,
    load_checkpoint,
    prepare_run_directory,
    rollback_to_checkpoint,
    save_checkpoint,
    write_json,
    write_output_manifest,
    write_profile,
    write_status,
    write_timeseries_csv,
    write_charge_timeseries,
)
from .runner import W10Runner
from .progress import Heartbeat, ProgressState
from .types import Checkpoint, ProtocolPhase, RunStatus, StageSpec, TerminationKind, NumericalFailure
from .udds import build_drive_window_plan


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _verify_lock_contention(run_dir: Path, workspace: Path) -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from pybamm_w10.output import RunDirectoryBusy, RunDirectoryLock\n"
        "try:\n"
        "    with RunDirectoryLock(Path(sys.argv[1]), {'mode':'smoke-competitor'}):\n"
        "        raise SystemExit(0)\n"
        "except RunDirectoryBusy:\n"
        "    raise SystemExit(17)\n"
    )
    environment = os.environ.copy()
    source = str(workspace / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", code, str(run_dir)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 17:
        raise RuntimeError(
            "second process was not rejected by the run-directory lock: "
            f"returncode={result.returncode}, stderr={result.stderr}"
        )


def run_smoke(config: RunConfig, output_dir: Path | None = None) -> Path:
    """Exercise real solve, restart, virtual isolation, rollback, and lock behavior."""
    workspace = Path.cwd()
    config = config.normalized(workspace)
    run_dir = (
        output_dir
        or config.output_root / f"smoke-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    ).resolve()
    with RunDirectoryLock(
        run_dir,
        {"mode": "smoke", "config_fingerprint": config.fingerprint(), "selected_checkpoint": None},
    ) as lock:
        existing = [path for path in run_dir.iterdir() if path.name != ".run.lock"]
        if existing:
            raise ValueError(f"smoke output directory is not empty: {run_dir}")
        heartbeat = Heartbeat(run_dir / "run_progress.json", interval_s=config.heartbeat_interval_s)
        heartbeat.start(ProgressState(phase="SMOKE_PREFLIGHT"))
        try:
            prepare_run_directory(run_dir)
            runner = W10Runner(config, workspace)
            base_udds, validation = runner.prepare_profile()
            artifacts = build_spme(config)
            environment = environment_metadata(artifacts)
            initial = construct_initial_state_record(artifacts, config)
            initial_fingerprint = initial.fingerprint
            input_fingerprint = _hash_file(config.w10_mat_path)
            environment_fingerprint = _fingerprint(environment)
            backend = PyBaMMBackend(artifacts, config.initial_soc, initial)
            audit = effective_parameters_audit(artifacts, config)
            effective_parameters_fingerprint_value = effective_parameters_fingerprint(audit)

            canonical_before = backend.snapshot()
            diagnostic = backend.fork()
            diagnostic.discharge_to_capacity(
                config.protocol.discharge_c4_a,
                diagnostic.discharge_capacity_ah(),
                0.0001,
                config.cell.lower_cutoff_v,
                spec=StageSpec(ProtocolPhase.STEP5_C4_DISCHARGE, TerminationKind.CAPACITY),
            )
            canonical_after = backend.snapshot()
            if canonical_after != canonical_before:
                raise RuntimeError("short virtual diagnostic changed the canonical main state")

            heartbeat.update(ProgressState(phase=ProtocolPhase.STEP5_C4_DISCHARGE.value))
            constant_outcome = backend.discharge_to_capacity(
                config.protocol.discharge_c4_a,
                backend.discharge_capacity_ah(),
                0.0002,
                config.cell.lower_cutoff_v,
                spec=StageSpec(ProtocolPhase.STEP5_C4_DISCHARGE, TerminationKind.CAPACITY),
            )
            if constant_outcome.termination_kind is not TerminationKind.CAPACITY:
                raise RuntimeError(
                    "short constant-current smoke did not end at its capacity event: "
                    f"kind={constant_outcome.termination_kind.value}, "
                    f"raw={constant_outcome.raw_termination!r}"
                )
            constant_event = {
                "termination_kind": constant_outcome.termination_kind.value,
                "raw_termination": constant_outcome.raw_termination,
                "event_time_s": constant_outcome.termination_time_s,
                "target_ah": 0.0002,
                "actual_ah": backend.discharge_capacity_ah(),
                "state_hash": constant_outcome.state_hash,
            }
            q_start = backend.discharge_capacity_ah()
            short_udds_target_ah = 0.0002
            drive = build_drive_window_plan(
                base_udds,
                short_udds_target_ah,
                config.solver.max_step_s,
                config,
            )
            heartbeat.update(ProgressState(phase=ProtocolPhase.STEP6_UDDS.value))
            udds_outcome = backend.drive_cycle_to_capacity(
                drive.profile,
                q_start,
                short_udds_target_ah,
                config.cell.lower_cutoff_v,
                spec=StageSpec(ProtocolPhase.STEP6_UDDS, TerminationKind.CAPACITY),
            )
            udds_duration = udds_outcome.termination_time_s - constant_outcome.termination_time_s
            if udds_outcome.termination_kind is not TerminationKind.CAPACITY:
                raise RuntimeError(
                    "short UDDS smoke did not end at its capacity event: "
                    f"kind={udds_outcome.termination_kind.value}, raw={udds_outcome.raw_termination!r}"
                )
            if not udds_duration < float(drive.profile.time_s[-1]):
                raise RuntimeError("short UDDS capacity event did not precede profile end")
            udds_actual = backend.discharge_capacity_ah() - q_start
            udds_relative_error = abs(udds_actual - short_udds_target_ah) / short_udds_target_ah
            if udds_relative_error > config.capacity_window_relative_tolerance:
                raise RuntimeError("short UDDS capacity error exceeds the production tolerance")
            udds_event = {
                "termination_kind": udds_outcome.termination_kind.value,
                "raw_termination": udds_outcome.raw_termination,
                "event_time_s": udds_duration,
                "profile_end_s": float(drive.profile.time_s[-1]),
                "target_ah": short_udds_target_ah,
                "actual_ah": udds_actual,
                "guard_ah": drive.guard_ah,
                "profile_available_ah": drive.profile_available_ah,
                "relative_error": udds_relative_error,
                "state_hash": udds_outcome.state_hash,
            }

            write_json(run_dir / "effective_parameters.json", audit)
            write_json(run_dir / "run_config.json", config.to_json())
            write_json(run_dir / "environment.json", environment)
            write_json(run_dir / "initial_state.json", {**asdict(initial), "fingerprint": initial_fingerprint})
            write_profile(run_dir / "udds_profile.csv", base_udds)
            write_json(run_dir / "udds_validation.json", validation)
            append_log(run_dir / "run.log", "committed smoke boundary")
            append_json_line(run_dir / "udds_cycle_validation.jsonl", {"smoke": "committed"})
            write_timeseries_csv(run_dir / "timeseries" / "checkpoint-state.csv", backend.timeseries())
            manifest = build_output_manifest(run_dir, 1, 0, None)
            checkpoint = Checkpoint(
                schema_version=config.checkpoint_schema_version,
                state=backend.snapshot(),
                aging_cycle=0,
                main_time_s=backend.current_time_s(),
                mode=config.mode,
                q_ref_ah=None,
                q_ref_node=None,
                initial_capacity_ah=None,
                protocol_phase=ProtocolPhase.CYCLE_COMPLETED,
                capacity_targets=None,
                config_fingerprint=config.fingerprint(),
                input_fingerprint=input_fingerprint,
                udds_fingerprint=base_udds.fingerprint,
                initial_state_fingerprint=initial_fingerprint,
                environment_fingerprint=environment_fingerprint,
                result_transaction=1,
                output_manifest=manifest,
                protocol_algorithm_version=config.protocol_algorithm_version,
                output_schema_version=config.output_schema_version,
                guard_config_fingerprint=config.guard_fingerprint(),
                last_successful_boundary="cycle-000",
                last_successful_stage=ProtocolPhase.CYCLE_COMPLETED,
                effective_parameters_fingerprint=effective_parameters_fingerprint_value,
                charge_efficiency_algorithm_version=config.charge_efficiency_algorithm_version,
                solver_execution_version=config.solver_execution_version,
            )
            checkpoint_path = run_dir / "checkpoints" / "cycle-000.pkl"
            save_checkpoint(checkpoint_path, checkpoint)
            write_output_manifest(run_dir / "output_manifest.json", manifest, checkpoint=checkpoint_path.name)

            continuous = backend.fork()
            continuous.discharge_to_capacity(
                config.protocol.discharge_c4_a,
                continuous.discharge_capacity_ah(),
                0.0001,
                config.cell.lower_cutoff_v,
            )

            append_log(run_dir / "run.log", "uncommitted smoke tail")
            append_json_line(run_dir / "udds_cycle_validation.jsonl", {"smoke": "uncommitted"})
            write_timeseries_csv(
                run_dir / "timeseries" / "uncommitted.csv",
                {"time_s": [0.0, 1.0], "current_a": [0.0, 0.0]},
            )
            (run_dir / "figures" / "uncommitted.png").write_bytes(b"uncommitted")
            (run_dir / "checkpoints" / "cycle-999.pkl").write_bytes(b"uncommitted")

            loaded = load_checkpoint(
                checkpoint_path,
                config,
                base_udds.fingerprint,
                input_fingerprint=input_fingerprint,
                initial_state_fingerprint=initial_fingerprint,
                environment_fingerprint=environment_fingerprint,
                effective_parameters_fingerprint=effective_parameters_fingerprint_value,
            )
            rollback = rollback_to_checkpoint(run_dir, checkpoint_path, loaded)
            second_rollback = rollback_to_checkpoint(run_dir, checkpoint_path, loaded)
            resumed = PyBaMMBackend(artifacts, config.initial_soc, initial)
            resumed.restore(loaded.state)
            resumed.discharge_to_capacity(
                config.protocol.discharge_c4_a,
                resumed.discharge_capacity_ah(),
                0.0001,
                config.cell.lower_cutoff_v,
            )
            if continuous.solution is None or resumed.solution is None:
                raise RuntimeError("restart comparison did not produce solutions")
            np.testing.assert_allclose(
                continuous.solution.y[:, -1],
                resumed.solution.y[:, -1],
                rtol=1e-9,
                atol=1e-11,
            )
            if abs(continuous.current_time_s() - resumed.current_time_s()) > 1e-9:
                raise RuntimeError("continuous and resumed times differ")
            if abs(continuous.discharge_capacity_ah() - resumed.discharge_capacity_ah()) > 1e-10:
                raise RuntimeError("continuous and resumed capacities differ")

            _verify_lock_contention(run_dir, workspace)
            if (run_dir / "cycle_summary.csv").exists() or any(
                path.name != "cycle-000.pkl" for path in (run_dir / "checkpoints").glob("cycle-*.pkl")
            ):
                raise RuntimeError("smoke must not write any aging-cycle result")
            report = {
                "status": "PASSED",
                "no_aging_cycles_executed": True,
                "canonical_initial_state_fingerprint": initial_fingerprint,
                "canonical_virtual_branch_unchanged": canonical_before == canonical_after,
                "constant_capacity_event": constant_event,
                "udds_capacity_event": udds_event,
                "restart": {
                    "continuous_state_hash": continuous.snapshot().state_hash,
                    "resumed_state_hash": resumed.snapshot().state_hash,
                    "time_s": resumed.current_time_s(),
                    "capacity_ah": resumed.discharge_capacity_ah(),
                },
                "rollback": rollback,
                "second_rollback_idempotent": (
                    not second_rollback["truncated_bytes"] and not second_rollback["moved_files"]
                ),
                "exclusive_lock_competitor_rejected": True,
            }
            write_json(run_dir / "smoke_report.json", report)
            write_status(run_dir / "run_status.json", RunStatus.COMPLETED, smoke=True)
            lock.set_business_status(RunStatus.COMPLETED)
            heartbeat.terminate(RunStatus.COMPLETED.value)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return run_dir
        except Exception as exc:
            write_status(
                run_dir / "run_status.json",
                RunStatus.NUMERICAL_FAILURE,
                smoke=True,
                exception_type=type(exc).__name__,
                error=str(exc),
            )
            lock.set_business_status(RunStatus.NUMERICAL_FAILURE)
            heartbeat.terminate(RunStatus.NUMERICAL_FAILURE.value)
            raise


def run_charge_efficiency_smoke(config: RunConfig, output_dir: Path | None = None) -> Path:
    """Run only the real four-stage charge window and emit schema-3 artifacts."""
    workspace = Path.cwd()
    config = config.normalized(workspace)
    run_dir = (output_dir or config.output_root / f"charge-efficiency-smoke-{datetime.now().strftime('%Y%m%dT%H%M%S')}").resolve()
    with RunDirectoryLock(run_dir, {"mode": "charge-efficiency-smoke", "config_fingerprint": config.fingerprint()}) as lock:
        if any(path.name != ".run.lock" for path in run_dir.iterdir()):
            raise ValueError(f"charge-efficiency smoke output directory is not empty: {run_dir}")
        prepare_run_directory(run_dir)
        runner = W10Runner(config, workspace)
        _, _ = runner.prepare_profile()
        artifacts = build_spme(config)
        inventory = preflight_charge_variables(artifacts.model, model_options=artifacts.options)
        inventory_path = write_charge_efficiency_variable_inventory(run_dir / "charge_efficiency_variable_inventory.json", inventory)
        inventory_sha256 = _hash_file(inventory_path)
        environment = environment_metadata(artifacts)
        initial = construct_initial_state_record(artifacts, config)
        backend = PyBaMMBackend(artifacts, config.initial_soc, initial)
        rpt = run_capacity_rpt(backend, 0, config, None, virtual=True)
        q_ref = rpt.capacity_ah
        sequence = backend.run_standard_charge_sequence(
            config, resolved_variables=inventory
        )
        expected = (
            TerminationKind.VOLTAGE,
            TerminationKind.CURRENT,
            TerminationKind.VOLTAGE,
            TerminationKind.CURRENT,
        )
        if tuple(outcome.termination_kind for outcome in sequence.outcomes) != expected:
            raise NumericalFailure("charge-efficiency smoke standard charge termination mismatch")
        traces = sequence.traces
        bundle = build_charge_analysis_bundle(
            tuple(traces), cycle=0, mode=config.mode, q_ref_ah=q_ref, q_ref_node=0, q_ref_initial_ah=q_ref,
            configured_charge_current_a=config.protocol.charge_3c_a, nominal_capacity_ah=config.cell.nominal_capacity_ah,
            faraday_constant_c_per_mol=config.faraday_constant_c_per_mol, soc_anchor_pct=config.soc_anchor_pct,
            balance_pass_limit_pct=config.charge_balance_pass_limit_pct, balance_failure_limit_pct=config.charge_balance_failure_limit_pct,
        )
        backend.commit_standard_charge_sequence(sequence)
        artifact = write_charge_timeseries(run_dir / "charge_timeseries" / "cycle-000.csv", tuple(dict(row) for row in bundle.trace_rows))
        relative = "charge_timeseries/cycle-000.csv"
        from dataclasses import replace
        summary = replace(bundle.summary, values={**bundle.summary.values, "charge_trace_path": relative, "charge_trace_sha256": artifact.sha256, "charge_trace_row_count": artifact.row_count})
        bins = tuple(replace(row, values={**row.values, "charge_trace_path": relative}) for row in bundle.soc_bins)
        append_charge_efficiency_summary(run_dir / "charge_efficiency_summary.csv", summary)
        append_charge_soc_bins(run_dir / "charge_efficiency_soc_bins.csv", bins)
        append_json_line(run_dir / "solver_attempts.jsonl", {
            "audit_version": config.solver_attempt_audit_version,
            "cycle": 0,
            "transaction": 0,
            "attempt_count": sequence.attempt_count,
            "solver_profile": sequence.solver_profile,
            "initial_failure_code": sequence.initial_failure_code,
            "final_status": "COMPLETED",
            "attempt_failures": [asdict(item) for item in sequence.attempt_failures],
        })
        write_json(run_dir / "run_config.json", config.to_json())
        write_json(run_dir / "environment.json", environment)
        write_json(run_dir / "initial_state.json", {**asdict(initial), "fingerprint": initial.fingerprint})
        manifest = build_output_manifest(run_dir, 1, 0, 0, last_charge_efficiency_cycle=0, last_complete_soc_bin_cycle=0)
        checkpoint = Checkpoint(
            schema_version=config.checkpoint_schema_version, state=backend.snapshot(), aging_cycle=0,
            main_time_s=backend.current_time_s(), mode=config.mode, q_ref_ah=q_ref, q_ref_node=0,
            initial_capacity_ah=q_ref, protocol_phase=ProtocolPhase.CYCLE_COMPLETED, capacity_targets=None,
            config_fingerprint=config.fingerprint(), input_fingerprint=_hash_file(config.w10_mat_path), udds_fingerprint="charge-efficiency-smoke",
            initial_state_fingerprint=initial.fingerprint, environment_fingerprint=_fingerprint(environment), result_transaction=1,
            output_manifest=manifest, protocol_algorithm_version=config.protocol_algorithm_version,
            output_schema_version=config.output_schema_version, guard_config_fingerprint=config.guard_fingerprint(),
            effective_parameters_fingerprint="", charge_efficiency_algorithm_version=config.charge_efficiency_algorithm_version,
            solver_execution_version=config.solver_execution_version,
            charge_efficiency_variable_inventory_sha256=inventory_sha256, last_charge_efficiency_cycle=0, last_complete_soc_bin_cycle=0,
        )
        checkpoint_path = run_dir / "checkpoints" / "cycle-000.pkl"
        save_checkpoint(checkpoint_path, checkpoint)
        write_output_manifest(run_dir / "output_manifest.json", manifest, checkpoint=checkpoint_path.name)
        report = {"status": "PASSED", "no_aging_cycles_executed": True, "external_charge_ah": summary.values["external_charge_ah"], "useful_charge_efficiency_pct": summary.values["useful_charge_efficiency_pct"], "trace_sha256": artifact.sha256}
        write_json(run_dir / "charge_efficiency_smoke_report.json", report)
        write_status(run_dir / "run_status.json", RunStatus.COMPLETED, smoke=True, charge_efficiency=True)
        lock.set_business_status(RunStatus.COMPLETED)
        return run_dir
