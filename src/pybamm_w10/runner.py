"""Formal run/resume scheduler with committed-output checkpoint semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import traceback
from typing import Any

from .backend import PyBaMMBackend, construct_initial_state_record
from .calibration.parameters import CalibrationParameters
from .config import RPT_NODES, RunConfig
from .charge_variables import (
    ResolvedChargeVariables,
    preflight_charge_variables,
    write_charge_efficiency_variable_inventory,
)
from .diagnostics import capacity_targets, run_capacity_rpt, run_validated_stage
from .model import (
    build_spme,
    effective_parameters_audit,
    effective_parameters_fingerprint,
    environment_metadata,
)
from .output import (
    RunDirectoryLock,
    append_dataclass,
    append_charge_efficiency_summary,
    append_charge_soc_bins,
    append_degradation_summary,
    append_json_line,
    append_log,
    build_output_manifest,
    load_checkpoint,
    prepare_run_directory,
    rollback_to_checkpoint,
    save_checkpoint,
    write_json,
    write_output_manifest,
    write_failure_artifacts,
    write_profile,
    write_status,
    write_timeseries_csv,
    write_charge_timeseries,
)
from .protocol import ProtocolStateMachine
from .progress import Heartbeat, ProgressState
from .types import (
    CapacityTargets,
    Checkpoint,
    FailureContext,
    FailureReason,
    NumericalFailure,
    PhysicalProtocolFailure,
    ProtocolPhase,
    RunStatus,
    StageSpec,
    TerminationKind,
    ChargeTraceArtifact,
)
from .udds import CurrentProfile, build_profile_from_mat


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def should_evaluate_full_soh(config: RunConfig) -> bool:
    """Return true only when the complete W10 RPT schedule was requested."""
    return (
        config.protocol.max_aging_cycles == RPT_NODES[-1]
        and config.protocol.rpt_nodes == RPT_NODES
    )


def ensure_required_interpreter(config: RunConfig) -> None:
    actual = os.path.normcase(str(Path(sys.executable).resolve()))
    expected = os.path.normcase(str(config.required_python.resolve()))
    if actual != expected:
        raise NumericalFailure(f"formal run requires interpreter {expected}; current interpreter is {actual}")


@dataclass
class W10Runner:
    config: RunConfig
    workspace: Path
    calibration_parameters: CalibrationParameters | None = None

    def prepare_profile(self) -> tuple[CurrentProfile, dict[str, object]]:
        return build_profile_from_mat(
            self.config.w10_mat_path,
            self.config.udds_period_min_s,
            self.config.udds_period_max_s,
        )

    def _checkpoint(
        self,
        backend: PyBaMMBackend,
        cycle: int,
        q_ref: float | None,
        q_node: int | None,
        initial_capacity: float | None,
        targets: CapacityTargets | None,
        profile: CurrentProfile,
        transaction: int,
        phase: ProtocolPhase,
        last_rpt_node: int | None,
        run_dir: Path,
        *,
        input_fingerprint: str,
        initial_state_fingerprint: str,
        environment_fingerprint: str,
        effective_parameters_fingerprint: str,
        charge_efficiency_inventory_sha256: str = "",
        last_charge_efficiency_cycle: int | None = None,
        last_complete_soc_bin_cycle: int | None = None,
    ) -> Path:
        manifest = build_output_manifest(
            run_dir, transaction, cycle, last_rpt_node,
            last_charge_efficiency_cycle=last_charge_efficiency_cycle,
            last_complete_soc_bin_cycle=last_complete_soc_bin_cycle,
        )
        checkpoint = Checkpoint(
            schema_version=self.config.checkpoint_schema_version,
            state=backend.snapshot(),
            aging_cycle=cycle,
            main_time_s=backend.current_time_s(),
            mode=self.config.mode,
            q_ref_ah=q_ref,
            q_ref_node=q_node,
            initial_capacity_ah=initial_capacity,
            protocol_phase=phase,
            capacity_targets=targets,
            config_fingerprint=self.config.fingerprint(),
            input_fingerprint=input_fingerprint,
            udds_fingerprint=profile.fingerprint,
            initial_state_fingerprint=initial_state_fingerprint,
            environment_fingerprint=environment_fingerprint,
            result_transaction=transaction,
            output_manifest=manifest,
            protocol_algorithm_version=self.config.protocol_algorithm_version,
            output_schema_version=self.config.output_schema_version,
            guard_config_fingerprint=self.config.guard_fingerprint(),
            last_successful_boundary=f"cycle-{cycle:03d}",
            last_successful_stage=phase,
            effective_parameters_fingerprint=effective_parameters_fingerprint,
            charge_efficiency_algorithm_version=self.config.charge_efficiency_algorithm_version,
            solver_execution_version=self.config.solver_execution_version,
            charge_efficiency_variable_inventory_sha256=charge_efficiency_inventory_sha256,
            last_charge_efficiency_cycle=last_charge_efficiency_cycle,
            last_complete_soc_bin_cycle=last_complete_soc_bin_cycle,
        )
        path = run_dir / "checkpoints" / f"cycle-{cycle:03d}.pkl"
        save_checkpoint(path, checkpoint)
        write_output_manifest(run_dir / "output_manifest.json", manifest, checkpoint=str(path.name))
        return path

    def run(
        self,
        output_dir: Path | None = None,
        *,
        resume_checkpoint: Path | None = None,
    ) -> RunStatus:
        """Run only after an explicit CLI run/resume request."""
        self.config = self.config.normalized(self.workspace)
        if resume_checkpoint is not None:
            checkpoint_path = resume_checkpoint.resolve()
            run_dir = checkpoint_path.parent.parent
            if output_dir is not None and output_dir.resolve() != run_dir.resolve():
                raise ValueError("--output-dir must match the selected checkpoint run directory")
            mode = "resume"
        else:
            checkpoint_path = None
            run_dir = (output_dir or self.config.output_root / datetime.now().strftime("%Y%m%dT%H%M%S")).resolve()
            mode = "run"
        lock_metadata = {
            "mode": mode,
            "config_fingerprint": self.config.fingerprint(),
            "selected_checkpoint": str(checkpoint_path) if checkpoint_path else None,
        }
        with RunDirectoryLock(run_dir, lock_metadata) as lock:
            heartbeat = Heartbeat(
                run_dir / "run_progress.json", interval_s=self.config.heartbeat_interval_s
            )
            terminal_status = RunStatus.NUMERICAL_FAILURE
            heartbeat.start(ProgressState(phase="PREFLIGHT"))
            try:
                prepare_run_directory(run_dir)
                if lock.stale_metadata and lock.stale_metadata.get("released_at_utc") is None:
                    append_json_line(run_dir / "lock_recovery_audit.jsonl", lock.stale_metadata)
                status = self._run_locked(run_dir, checkpoint_path, heartbeat)
                terminal_status = status
                lock.set_business_status(status)
                return status
            except Exception as exc:
                lock.set_business_status(RunStatus.NUMERICAL_FAILURE)
                if checkpoint_path is None:
                    write_status(
                        run_dir / "run_status.json",
                        RunStatus.NUMERICAL_FAILURE,
                        completed_aging_cycles=0,
                        exception_type=type(exc).__name__,
                        error=str(exc),
                        traceback=traceback.format_exc(),
                        phase="PREFLIGHT",
                    )
                raise
            finally:
                heartbeat.terminate(terminal_status.value)

    def _run_locked(
        self, run_dir: Path, checkpoint_path: Path | None, heartbeat: Heartbeat
    ) -> RunStatus:
        ensure_required_interpreter(self.config)
        if not self.config.w10_mat_path.is_file():
            raise NumericalFailure(f"W10 input file does not exist: {self.config.w10_mat_path}")
        input_fingerprint = _hash_file(self.config.w10_mat_path)
        base_udds, validation = self.prepare_profile()
        artifacts = build_spme(self.config, self.calibration_parameters)
        charge_inventory = preflight_charge_variables(artifacts.model, model_options=artifacts.options)
        inventory_path = run_dir / "charge_efficiency_variable_inventory.json"
        if checkpoint_path is None:
            write_charge_efficiency_variable_inventory(inventory_path, charge_inventory)
        elif not inventory_path.is_file():
            raise NumericalFailure("resume requires charge_efficiency_variable_inventory.json")
        charge_inventory_sha256 = _hash_file(inventory_path)
        environment = environment_metadata(artifacts)
        environment_fingerprint = _fingerprint(environment)
        initial_state = construct_initial_state_record(artifacts, self.config)
        initial_state_fingerprint = initial_state.fingerprint
        required_variables = (
            "Terminal voltage [V]",
            "Current [A]",
            "Discharge capacity [A.h]",
            "X-averaged cell temperature [K]",
            "Loss of capacity to negative SEI [A.h]",
            "Loss of capacity to negative lithium plating [A.h]",
            "Volume-averaged negative dead lithium concentration [mol.m-3]",
            "Loss of active material in negative electrode [%]",
            "Loss of active material in positive electrode [%]",
            "X-averaged negative electrode porosity",
            "X-averaged positive electrode porosity",
            "X-averaged negative electrode active material volume fraction",
            "X-averaged positive electrode active material volume fraction",
        )
        missing = [name for name in required_variables if name not in artifacts.model.variables]
        if missing:
            raise NumericalFailure(f"processed SPMe is missing required variables: {missing}")

        backend = PyBaMMBackend(artifacts, self.config.initial_soc, initial_state)
        q_ref: float | None = None
        q_ref_node: int | None = None
        initial_capacity: float | None = None
        targets: CapacityTargets | None = None
        transaction = 0
        completed_cycle = 0
        last_rpt_node: int | None = None
        phase = ProtocolPhase.INITIAL_RPT

        def report_phase(
            value: ProtocolPhase,
            *,
            current_cycle: int | None = None,
            stage: str | None = None,
            solver_attempt: int = 1,
            solver_profile: str = "general_protocol",
        ) -> None:
            heartbeat.update(
                ProgressState(
                    phase=value.value,
                    stage=stage,
                    completed_cycles=completed_cycle,
                    transaction=transaction,
                    current_cycle=current_cycle,
                    solver_attempt=solver_attempt,
                    solver_profile=solver_profile,
                )
            )

        protocol = ProtocolStateMachine(
            self.config,
            base_udds,
            on_stage_change=lambda value, stage: report_phase(
                value,
                current_cycle=completed_cycle + 1,
                stage=stage,
            ),
            on_solver_stage_change=lambda value, stage, attempt, profile: report_phase(
                value,
                current_cycle=completed_cycle + 1,
                stage=stage,
                solver_attempt=attempt,
                solver_profile=profile,
            ),
            resolved_charge_variables=charge_inventory,
        )

        if checkpoint_path is None:
            audit = effective_parameters_audit(
                artifacts, self.config, calibration_parameters=self.calibration_parameters
            )
            effective_parameters_fingerprint_value = effective_parameters_fingerprint(audit)
            write_json(run_dir / "effective_parameters.json", audit)
            write_json(run_dir / "run_config.json", self.config.to_json())
            write_json(run_dir / "environment.json", environment)
            write_json(run_dir / "initial_state.json", {**asdict(initial_state), "fingerprint": initial_state_fingerprint})
            write_profile(run_dir / "udds_profile.csv", base_udds)
            write_json(run_dir / "udds_validation.json", validation)
            append_log(run_dir / "run.log", "W10 PyBaMM run started")
        else:
            audit_path = run_dir / "effective_parameters.json"
            if not audit_path.is_file():
                raise NumericalFailure("resume requires effective_parameters.json")
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            effective_parameters_fingerprint_value = effective_parameters_fingerprint(audit)
            if audit.get("fingerprint") != effective_parameters_fingerprint_value:
                raise NumericalFailure("effective parameter audit fingerprint is invalid")
            checkpoint = load_checkpoint(
                checkpoint_path,
                self.config,
                base_udds.fingerprint,
                input_fingerprint=input_fingerprint,
                initial_state_fingerprint=initial_state_fingerprint,
                environment_fingerprint=environment_fingerprint,
                effective_parameters_fingerprint=effective_parameters_fingerprint_value,
                charge_efficiency_inventory_sha256=charge_inventory_sha256,
            )
            rollback_to_checkpoint(run_dir, checkpoint_path, checkpoint)
            backend.restore(checkpoint.state)
            q_ref, q_ref_node = checkpoint.q_ref_ah, checkpoint.q_ref_node
            initial_capacity = checkpoint.initial_capacity_ah
            targets = checkpoint.capacity_targets
            transaction = checkpoint.result_transaction
            completed_cycle = checkpoint.aging_cycle
            last_rpt_node = checkpoint.output_manifest.last_rpt_node
            phase = checkpoint.protocol_phase
            append_log(
                run_dir / "run.log",
                f"resume checkpoint={checkpoint_path.name} transaction={transaction} phase={phase.value}",
            )
            if phase == ProtocolPhase.RUN_COMPLETED:
                return RunStatus.COMPLETED

        try:
            if phase == ProtocolPhase.INITIAL_RPT:
                report_phase(phase, current_cycle=0, stage="initial_rpt")
                rpt = run_capacity_rpt(
                    backend, 0, self.config, None, virtual=self.config.mode == "virtual"
                )
                initial_capacity = q_ref = rpt.capacity_ah
                q_ref_node = 0
                targets = capacity_targets(q_ref, config=self.config)
                audit = effective_parameters_audit(
                    artifacts,
                    self.config,
                    cycle_0_capacity_ah=initial_capacity,
                    calibration_parameters=self.calibration_parameters,
                )
                effective_parameters_fingerprint_value = effective_parameters_fingerprint(audit)
                write_json(run_dir / "effective_parameters.json", audit)
                append_dataclass(run_dir / "rpt_summary.csv", rpt)
                write_timeseries_csv(run_dir / "timeseries" / "rpt-node-000.csv", rpt.timeseries)
                backend.compact_state()
                transaction += 1
                last_rpt_node = 0
                phase = (
                    ProtocolPhase.POST_RPT_RECOVERY
                    if self.config.mode == "strict-w10"
                    else ProtocolPhase.CYCLE_COMPLETED
                )
                self._checkpoint(
                    backend, 0, q_ref, q_ref_node, initial_capacity, targets, base_udds,
                    transaction, phase, last_rpt_node, run_dir,
                    input_fingerprint=input_fingerprint,
                    initial_state_fingerprint=initial_state_fingerprint,
                    environment_fingerprint=environment_fingerprint,
                    effective_parameters_fingerprint=effective_parameters_fingerprint_value,
                    charge_efficiency_inventory_sha256=charge_inventory_sha256,
                )

            pending_recovery: tuple[float, dict[str, float], dict[str, float]] | None = None
            if phase == ProtocolPhase.RPT_PRECONDITIONING:
                report_phase(
                    phase,
                    current_cycle=completed_cycle,
                    stage="rpt_preconditioning",
                )
                if completed_cycle not in self.config.protocol.rpt_nodes:
                    raise NumericalFailure("checkpoint requests an RPT at a non-RPT cycle")
                q_ref, q_ref_node, targets, transaction, last_rpt_node, phase = self._run_rpt_transaction(
                    backend, completed_cycle, q_ref, q_ref_node, initial_capacity, base_udds,
                    transaction, last_rpt_node, run_dir, input_fingerprint,
                    initial_state_fingerprint, environment_fingerprint,
                    effective_parameters_fingerprint_value, charge_inventory_sha256, heartbeat,
                )
            if phase == ProtocolPhase.POST_RPT_RECOVERY:
                report_phase(
                    phase,
                    current_cycle=completed_cycle,
                    stage="post_rpt_recovery",
                )
                pending_recovery = self._post_rpt_recovery(
                    backend, completed_cycle, charge_inventory
                )

            cycle = completed_cycle + 1
            while cycle <= self.config.protocol.max_aging_cycles:
                if q_ref is None or q_ref_node is None:
                    raise NumericalFailure("aging cycle has no successful frozen Q_ref")
                protocol.q_ref_initial_ah = initial_capacity
                result = protocol.run_standard_cycle(
                    backend,
                    cycle,
                    q_ref,
                    q_ref_node,
                    charge_already_complete=pending_recovery is not None,
                    cycle_start_time_s=pending_recovery[0] if pending_recovery else None,
                    precompleted_stage_durations=pending_recovery[1] if pending_recovery else None,
                    precompleted_metrics=pending_recovery[2] if pending_recovery else None,
                )
                pending_recovery = None
                append_json_line(
                    run_dir / "solver_attempts.jsonl",
                    {
                        "audit_version": self.config.solver_attempt_audit_version,
                        "cycle": cycle,
                        "transaction": transaction,
                        "attempt_count": result.solver_attempt_count,
                        "solver_profile": result.solver_profile,
                        "initial_failure_code": result.initial_solver_failure_code,
                        "final_status": "COMPLETED",
                        "attempt_failures": [asdict(item) for item in result.solver_attempt_failures],
                    },
                )
                self._write_charge_analysis(result, run_dir)
                append_dataclass(run_dir / "cycle_summary.csv", result)
                append_degradation_summary(run_dir / "degradation_summary.csv", result)
                if result.charge_analysis is not None:
                    append_charge_efficiency_summary(
                        run_dir / "charge_efficiency_summary.csv", result.charge_analysis.summary
                    )
                    append_charge_soc_bins(
                        run_dir / "charge_efficiency_soc_bins.csv", result.charge_analysis.soc_bins
                    )
                self._record_cycle_validation(run_dir, result)
                self._write_retained_timeseries(backend, result.start_time_s, cycle, run_dir)
                backend.compact_state()
                transaction += 1
                completed_cycle = cycle
                targets = capacity_targets(q_ref, config=self.config)

                if cycle in self.config.protocol.rpt_nodes:
                    self._checkpoint(
                        backend, cycle, q_ref, q_ref_node, initial_capacity, targets, base_udds,
                        transaction, ProtocolPhase.RPT_PRECONDITIONING, last_rpt_node, run_dir,
                        input_fingerprint=input_fingerprint,
                    initial_state_fingerprint=initial_state_fingerprint,
                    environment_fingerprint=environment_fingerprint,
                    effective_parameters_fingerprint=effective_parameters_fingerprint_value,
                    charge_efficiency_inventory_sha256=charge_inventory_sha256,
                    last_charge_efficiency_cycle=cycle,
                    last_complete_soc_bin_cycle=cycle,
                    )
                    q_ref, q_ref_node, targets, transaction, last_rpt_node, phase = self._run_rpt_transaction(
                        backend, cycle, q_ref, q_ref_node, initial_capacity, base_udds,
                        transaction, last_rpt_node, run_dir, input_fingerprint,
                        initial_state_fingerprint, environment_fingerprint,
                        effective_parameters_fingerprint_value, charge_inventory_sha256, heartbeat,
                    )
                    if phase == ProtocolPhase.POST_RPT_RECOVERY:
                        pending_recovery = self._post_rpt_recovery(
                            backend, completed_cycle, charge_inventory
                        )
                elif cycle % self.config.checkpoint_every_cycles == 0:
                    self._checkpoint(
                        backend, cycle, q_ref, q_ref_node, initial_capacity, targets, base_udds,
                        transaction, ProtocolPhase.CYCLE_COMPLETED, last_rpt_node, run_dir,
                        input_fingerprint=input_fingerprint,
                        initial_state_fingerprint=initial_state_fingerprint,
                        environment_fingerprint=environment_fingerprint,
                        effective_parameters_fingerprint=effective_parameters_fingerprint_value,
                        charge_efficiency_inventory_sha256=charge_inventory_sha256,
                        last_charge_efficiency_cycle=cycle,
                        last_complete_soc_bin_cycle=cycle,
                    )
                cycle += 1

            if should_evaluate_full_soh(self.config):
                from .evaluation import evaluate_soh_comparison
                from .figures import generate_figures

                evaluate_soh_comparison(
                    run_dir,
                    self.config.data_root,
                    require_completed_status=False,
                )
                generate_figures(run_dir, self.config.w10_mat_path, self.config.output_root)
            write_status(
                run_dir / "run_status.json",
                RunStatus.COMPLETED,
                completed_aging_cycles=completed_cycle,
                transaction=transaction + 1,
            )
            transaction += 1
            self._checkpoint(
                backend, completed_cycle, q_ref, q_ref_node, initial_capacity, targets, base_udds,
                transaction, ProtocolPhase.RUN_COMPLETED, last_rpt_node, run_dir,
                input_fingerprint=input_fingerprint,
                initial_state_fingerprint=initial_state_fingerprint,
                environment_fingerprint=environment_fingerprint,
                effective_parameters_fingerprint=effective_parameters_fingerprint_value,
                charge_efficiency_inventory_sha256=charge_inventory_sha256,
            )
            return RunStatus.COMPLETED
        except PhysicalProtocolFailure as exc:
            context = exc.context
            try:
                write_failure_artifacts(run_dir, context)
            except Exception:
                pass
            write_status(
                run_dir / "run_status.json",
                RunStatus.PHYSICAL_PROTOCOL_FAILURE,
                completed_aging_cycles=completed_cycle,
                reason=context.reason.value,
                failure_context=context.to_json(),
                error=str(exc),
                last_termination=backend.last_termination,
                last_valid_transaction=transaction,
            )
            return RunStatus.PHYSICAL_PROTOCOL_FAILURE
        except Exception as exc:
            existing = getattr(exc, "context", None)
            context = existing if isinstance(existing, FailureContext) else FailureContext(
                reason=FailureReason.OUTPUT_FAILURE if isinstance(exc, OSError) else FailureReason.SOLVER_FAILURE,
                mode=self.config.mode,
                cycle=completed_cycle + 1,
                phase=phase,
                exception_type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
            )
            checkpoint_candidate = run_dir / "checkpoints" / f"cycle-{completed_cycle:03d}.pkl"
            if checkpoint_candidate.is_file():
                try:
                    load_checkpoint(
                        checkpoint_candidate,
                        self.config,
                        base_udds.fingerprint,
                        input_fingerprint=input_fingerprint,
                        initial_state_fingerprint=initial_state_fingerprint,
                        environment_fingerprint=environment_fingerprint,
                        effective_parameters_fingerprint=effective_parameters_fingerprint_value,
                        charge_efficiency_inventory_sha256=charge_inventory_sha256,
                    )
                except Exception:
                    pass
                else:
                    relative_checkpoint = checkpoint_candidate.relative_to(run_dir).as_posix()
                    context = replace(
                        context,
                        last_checkpoint=relative_checkpoint,
                        last_committed_checkpoint=relative_checkpoint,
                        resume_checkpoint=relative_checkpoint,
                        resume_eligible=True,
                    )
            if context.attempt_failures:
                try:
                    append_json_line(
                        run_dir / "solver_attempts.jsonl",
                        {
                            "audit_version": self.config.solver_attempt_audit_version,
                            "cycle": context.cycle,
                            "transaction": transaction,
                            "attempt_count": len(context.attempt_failures),
                            "solver_profile": context.solver_profile,
                            "initial_failure_code": context.attempt_failures[0].get("sundials_error_code"),
                            "final_status": "NUMERICAL_FAILURE",
                            "attempt_failures": list(context.attempt_failures),
                        },
                    )
                except OSError:
                    pass
            try:
                write_failure_artifacts(run_dir, context)
            except Exception:
                pass
            write_status(
                run_dir / "run_status.json",
                RunStatus.NUMERICAL_FAILURE,
                completed_aging_cycles=completed_cycle,
                reason=context.reason.value,
                failure_context=context.to_json(),
                exception_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
                last_valid_transaction=transaction,
            )
            return RunStatus.NUMERICAL_FAILURE

    def _run_rpt_transaction(
        self,
        backend: PyBaMMBackend,
        node: int,
        q_ref: float | None,
        q_ref_node: int | None,
        initial_capacity: float | None,
        base_udds: CurrentProfile,
        transaction: int,
        last_rpt_node: int | None,
        run_dir: Path,
        input_fingerprint: str,
        initial_state_fingerprint: str,
        environment_fingerprint: str,
        effective_parameters_fingerprint_value: str = "",
        charge_efficiency_inventory_sha256: str = "",
        heartbeat: Heartbeat | None = None,
    ) -> tuple[float | None, int | None, CapacityTargets | None, int, int, ProtocolPhase]:
        if heartbeat is not None:
            heartbeat.update(
                ProgressState(
                    phase=ProtocolPhase.RPT_PRECONDITIONING.value,
                    stage="rpt_preconditioning",
                    completed_cycles=node,
                    transaction=transaction,
                    current_cycle=node,
                )
            )
        rpt = run_capacity_rpt(
            backend,
            node,
            self.config,
            initial_capacity,
            virtual=self.config.mode == "virtual",
        )
        append_dataclass(run_dir / "rpt_summary.csv", rpt)
        write_timeseries_csv(run_dir / "timeseries" / f"rpt-node-{node:03d}.csv", rpt.timeseries)
        backend.compact_state()
        transaction += 1
        last_rpt_node = node
        if node < self.config.protocol.max_aging_cycles:
            q_ref, q_ref_node = rpt.capacity_ah, node
            targets = capacity_targets(q_ref, config=self.config)
            phase = (
                ProtocolPhase.POST_RPT_RECOVERY
                if self.config.mode == "strict-w10"
                else ProtocolPhase.CYCLE_COMPLETED
            )
        else:
            targets = None
            phase = ProtocolPhase.CYCLE_COMPLETED
        self._checkpoint(
            backend, node, q_ref, q_ref_node, initial_capacity, targets, base_udds,
            transaction, phase, last_rpt_node, run_dir,
            input_fingerprint=input_fingerprint,
            initial_state_fingerprint=initial_state_fingerprint,
            environment_fingerprint=environment_fingerprint,
            effective_parameters_fingerprint=effective_parameters_fingerprint_value,
            charge_efficiency_inventory_sha256=charge_efficiency_inventory_sha256,
            last_charge_efficiency_cycle=node if node else None,
            last_complete_soc_bin_cycle=node if node else None,
        )
        if heartbeat is not None:
            heartbeat.update(
                ProgressState(
                    phase=phase.value,
                    stage="post_rpt_recovery" if phase == ProtocolPhase.POST_RPT_RECOVERY else "rpt_completed",
                    completed_cycles=node,
                    transaction=transaction,
                    current_cycle=node,
                )
            )
        return q_ref, q_ref_node, targets, transaction, last_rpt_node, phase

    def _post_rpt_recovery(
        self,
        backend: PyBaMMBackend,
        rpt_node: int,
        resolved_charge_variables: ResolvedChargeVariables | None = None,
    ) -> tuple[float, dict[str, float], dict[str, float]]:
        started = backend.current_time_s()
        durations: dict[str, float] = {}
        charge_minima: list[float] = []
        before = backend.current_time_s()
        run_validated_stage(
            lambda spec: backend.cc_charge_to_voltage(
                self.config.cell.nominal_capacity_ah, self.config.cell.upper_cutoff_v, spec=spec
            ),
            StageSpec(
                ProtocolPhase.POST_RPT_RECOVERY,
                TerminationKind.VOLTAGE,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
            ),
            mode=self.config.mode,
            rpt_node=rpt_node,
        )
        durations["rpt_recovery_cc"] = backend.current_time_s() - before
        if resolved_charge_variables is not None:
            trace = backend.extract_charge_stage_trace(
                "rpt_recovery_cc", before, backend.current_time_s(), resolved_charge_variables
            )
            charge_minima.extend(
                trace.values["negative_electrode_surface_potential_difference_v"]
            )
        before = backend.current_time_s()
        run_validated_stage(
            lambda spec: backend.cv_hold_to_current(
                self.config.cell.upper_cutoff_v, self.config.protocol.cv_cutoff_a, spec=spec
            ),
            StageSpec(
                ProtocolPhase.POST_RPT_RECOVERY,
                TerminationKind.CURRENT,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
            ),
            mode=self.config.mode,
            rpt_node=rpt_node,
        )
        durations["rpt_recovery_cv"] = backend.current_time_s() - before
        if resolved_charge_variables is not None:
            trace = backend.extract_charge_stage_trace(
                "rpt_recovery_cv", before, backend.current_time_s(), resolved_charge_variables
            )
            charge_minima.extend(
                trace.values["negative_electrode_surface_potential_difference_v"]
            )
        before = backend.current_time_s()
        run_validated_stage(
            lambda spec: backend.rest(self.config.protocol.rpt_rest_s, spec=spec),
            StageSpec(
                ProtocolPhase.POST_RPT_RECOVERY,
                TerminationKind.FINAL_TIME,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
            ),
            mode=self.config.mode,
            rpt_node=rpt_node,
        )
        durations["rpt_recovery_rest"] = backend.current_time_s() - before
        metrics = (
            {"negative_electrode_min_potential_v": min(charge_minima)}
            if charge_minima
            else {}
        )
        return started, durations, metrics

    def _write_retained_timeseries(
        self, backend: PyBaMMBackend, start_time_s: float, cycle: int, run_dir: Path
    ) -> None:
        if cycle in self.config.retained_timeseries_cycles:
            trace = backend.timeseries_since(start_time_s)
            if trace:
                write_timeseries_csv(run_dir / "timeseries" / f"cycle-{cycle:03d}.csv", trace)

    @staticmethod
    def _write_charge_analysis(result: Any, run_dir: Path) -> None:
        """Persist the trace first, then attach its immutable identity to all rows."""
        bundle = result.charge_analysis
        if bundle is None:
            return
        if not bundle.trace_rows:
            return
        target = run_dir / "charge_timeseries" / f"cycle-{result.cycle:03d}.csv"
        artifact = write_charge_timeseries(target, tuple(dict(row) for row in bundle.trace_rows))
        relative = target.relative_to(run_dir).as_posix()
        artifact = ChargeTraceArtifact(relative, artifact.sha256, artifact.row_count, artifact.start_time_s, artifact.end_time_s)
        summary = replace(bundle.summary, values={
            **bundle.summary.values,
            "charge_trace_path": relative,
            "charge_trace_sha256": artifact.sha256,
            "charge_trace_row_count": artifact.row_count,
        })
        bins = tuple(replace(row, values={
            **row.values,
            "charge_trace_path": relative,
            "trace_start_time_s": artifact.start_time_s,
            "trace_end_time_s": artifact.end_time_s,
        }) for row in bundle.soc_bins)
        result.charge_analysis = replace(bundle, summary=summary, soc_bins=bins, trace_artifact=artifact)

    @staticmethod
    def _record_cycle_validation(run_dir: Path, result: Any) -> None:
        evidence = {
            "cycle": result.cycle,
            "step5_target_ah": result.step5_target_ah,
            "step5_actual_ah": result.delta_q5_actual_ah,
            "step5_relative_error": result.metrics["step5_relative_error"],
            "window_target_ah": result.window_target_ah,
            "window_actual_ah": result.window_actual_ah,
            "window_relative_error": abs(result.window_actual_ah - result.window_target_ah)
            / result.window_target_ah,
            "udds_remaining_target_ah": result.actual_udds_remaining_target_ah,
            "udds_profile_net_discharge_ah": result.metrics["udds_profile_net_discharge_ah"],
            "udds_profile_target_error": result.metrics["udds_profile_target_error"],
        }
        append_json_line(run_dir / "udds_cycle_validation.jsonl", evidence)
        append_log(
            run_dir / "run.log",
            f"cycle={result.cycle} UDDS window error={evidence['window_relative_error']:.3e}",
        )
