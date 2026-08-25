"""One W10 aging-protocol state machine for virtual and strict-W10 modes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from time import monotonic
from typing import Any, Callable, Protocol

import numpy as np

from .config import RunConfig
from .charge_efficiency import build_charge_analysis_bundle, build_skipped_charge_analysis_bundle
from .charge_variables import ResolvedChargeVariables
from .diagnostics import capacity_targets
from .types import (
    CycleResult,
    FailureContext,
    FailureReason,
    NumericalFailure,
    PhysicalProtocolFailure,
    ProtocolPhase,
    StageOutcome,
    StageSpec,
    SolverStepFailure,
    TerminationKind,
)
from .udds import CurrentProfile, build_drive_window_plan


class AgingBackend(Protocol):
    def current_time_s(self) -> float: ...
    def discharge_capacity_ah(self) -> float: ...
    def cc_charge_to_voltage(self, current_a: float, voltage_v: float, *, spec: StageSpec) -> StageOutcome: ...
    def cv_hold_to_current(self, voltage_v: float, cutoff_current_a: float, *, spec: StageSpec) -> StageOutcome: ...
    def rest(self, duration_s: float, *, spec: StageSpec) -> StageOutcome: ...
    def discharge_to_capacity(self, current_a: float, q_start_ah: float, target_ah: float, voltage_v: float, *, spec: StageSpec) -> StageOutcome: ...
    def drive_cycle_to_capacity(self, profile: CurrentProfile, q_start_ah: float, target_ah: float, voltage_v: float, *, spec: StageSpec) -> StageOutcome: ...
    def summary_metrics(self, start_time_s: float | None = None) -> dict[str, float]: ...
    def run_standard_charge_sequence(self, config: RunConfig, **kwargs: Any): ...


def state_digest(solution: Any) -> str:
    """A stable evidence hash for virtual-RPT non-invasiveness checks."""
    if solution is None:
        return "initial"
    values = getattr(solution, "y", None)
    if values is None:
        return sha256(repr(solution).encode()).hexdigest()
    return sha256(np.ascontiguousarray(values[:, -1]).tobytes()).hexdigest()


@dataclass
class ProtocolStateMachine:
    config: RunConfig
    base_udds: CurrentProfile
    current_phase: ProtocolPhase | None = None
    on_phase_change: Callable[[ProtocolPhase], None] | None = None
    on_stage_change: Callable[[ProtocolPhase, str], None] | None = None
    on_solver_stage_change: Callable[[ProtocolPhase, str, int, str], None] | None = None
    resolved_charge_variables: ResolvedChargeVariables | None = None
    q_ref_initial_ah: float | None = None

    def run_standard_cycle(
        self,
        backend: AgingBackend,
        cycle: int,
        q_ref_ah: float,
        q_ref_node: int,
        *,
        charge_already_complete: bool = False,
        cycle_start_time_s: float | None = None,
        precompleted_stage_durations: dict[str, float] | None = None,
        precompleted_metrics: dict[str, float] | None = None,
    ) -> CycleResult:
        """Run one complete aging cycle; indexing changes only after UDDS terminates."""
        protocol = self.config.protocol
        started = backend.current_time_s() if cycle_start_time_s is None else cycle_start_time_s
        durations: dict[str, float] = dict(precompleted_stage_durations or {})
        wall_durations: dict[str, float] = {}
        simulated_starts: dict[str, float] = {}
        active_context_values: dict[str, Any] = {}
        charge_traces = []
        solver_attempt_count = 1
        solver_profile = "general_protocol"
        initial_solver_failure_code: str | None = None
        solver_attempt_failures = ()

        def failure_context(
            reason: FailureReason,
            phase: ProtocolPhase,
            outcome: StageOutcome | None = None,
            **values: Any,
        ) -> FailureContext:
            payload = dict(active_context_values)
            payload.update(values)
            return FailureContext(
                reason=reason,
                mode=self.config.mode,
                cycle=cycle,
                rpt_node=q_ref_node,
                phase=phase,
                q_ref_ah=q_ref_ah,
                q_ref_node=q_ref_node,
                termination_kind=None if outcome is None else outcome.termination_kind,
                raw_termination=None if outcome is None else outcome.raw_termination,
                termination_time_s=None if outcome is None else outcome.termination_time_s,
                termination_value=None if outcome is None else outcome.termination_value,
                terminal_voltage_v=None if outcome is None else outcome.terminal_voltage_v,
                terminal_temperature_k=None if outcome is None else outcome.terminal_temperature_k,
                terminal_discharge_capacity_ah=None if outcome is None else outcome.terminal_discharge_capacity_ah,
                state_hash=None if outcome is None else outcome.state_hash,
                **payload,
            )

        def valid_outcome(outcome: StageOutcome) -> bool:
            scalars = (
                outcome.termination_time_s,
                outcome.terminal_voltage_v,
                outcome.terminal_temperature_k,
                outcome.terminal_discharge_capacity_ah,
            )
            return bool(outcome.state_hash) and all(
                value is not None and np.isfinite(value) for value in scalars
            )

        def stage(name: str, spec: StageSpec, call) -> StageOutcome:
            self.current_phase = spec.phase
            if self.on_phase_change is not None:
                self.on_phase_change(spec.phase)
            if self.on_stage_change is not None:
                self.on_stage_change(spec.phase, name)
            before = backend.current_time_s()
            simulated_starts[name] = before
            wall_before = monotonic()
            try:
                outcome = call(spec)
            except (PhysicalProtocolFailure, NumericalFailure):
                raise
            except Exception as exc:
                raise NumericalFailure(
                    failure_context(
                        FailureReason.SOLVER_FAILURE,
                        spec.phase,
                        exception_type=type(exc).__name__,
                        message=str(exc),
                    )
                ) from exc
            wall_durations[name] = monotonic() - wall_before
            durations[name] = backend.current_time_s() - before
            if not isinstance(outcome, StageOutcome) or not valid_outcome(outcome):
                raise NumericalFailure(
                    failure_context(FailureReason.INVALID_STATE, spec.phase, outcome)
                )
            if outcome.termination_kind is spec.expected_termination:
                if (
                    name in {"3c_cc", "4v_cv", "c4_cc", "4p2v_cv"}
                    and self.resolved_charge_variables is not None
                    and hasattr(backend, "extract_charge_stage_trace")
                ):
                    try:
                        charge_traces.append(
                            backend.extract_charge_stage_trace(
                                name, before, backend.current_time_s(), self.resolved_charge_variables
                            )
                        )
                    except Exception as exc:
                        if isinstance(exc, NumericalFailure):
                            raise
                        raise NumericalFailure(failure_context(
                            FailureReason.INVALID_STATE, spec.phase, outcome,
                            charge_stage=name, exception_type=type(exc).__name__, message=str(exc),
                        )) from exc
                return outcome
            if outcome.termination_kind in spec.allowed_physical_terminations:
                raise PhysicalProtocolFailure(
                    failure_context(FailureReason.PHYSICAL_EVENT_BEFORE_TARGET, spec.phase, outcome)
                )
            if outcome.termination_kind is TerminationKind.FINAL_TIME:
                raise NumericalFailure(
                    failure_context(FailureReason.UNEXPECTED_FINAL_TIME, spec.phase, outcome)
                )
            raise NumericalFailure(
                failure_context(FailureReason.UNKNOWN_TERMINATION, spec.phase, outcome)
            )

        if not charge_already_complete:
            charge_specs = (
                StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.VOLTAGE, (TerminationKind.MODEL_PHYSICAL_EVENT,)),
                StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.CURRENT, (TerminationKind.MODEL_PHYSICAL_EVENT,)),
                StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.VOLTAGE, (TerminationKind.MODEL_PHYSICAL_EVENT,)),
                StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.CURRENT, (TerminationKind.MODEL_PHYSICAL_EVENT,)),
            )
            charge_names = ("3c_cc", "4v_cv", "c4_cc", "4p2v_cv")

            def report_charge_stage(name: str, attempt: int, profile: str) -> None:
                self.current_phase = ProtocolPhase.STANDARD_CHARGE
                if self.on_phase_change is not None:
                    self.on_phase_change(ProtocolPhase.STANDARD_CHARGE)
                if self.on_solver_stage_change is not None:
                    self.on_solver_stage_change(
                        ProtocolPhase.STANDARD_CHARGE, name, attempt, profile
                    )
                elif self.on_stage_change is not None:
                    self.on_stage_change(ProtocolPhase.STANDARD_CHARGE, name)

            try:
                sequence = backend.run_standard_charge_sequence(
                    self.config,
                    resolved_variables=self.resolved_charge_variables,
                    on_stage_change=report_charge_stage,
                )
            except (PhysicalProtocolFailure, NumericalFailure):
                raise
            except SolverStepFailure as exc:
                raise NumericalFailure(failure_context(
                    FailureReason.SOLVER_FAILURE,
                    ProtocolPhase.STANDARD_CHARGE,
                    charge_stage=exc.charge_stage,
                    failed_step_index=exc.failed_step_index,
                    solver_attempt=len(exc.attempt_failures) or 1,
                    solver_profile=(
                        exc.attempt_failures[-1].solver_profile
                        if exc.attempt_failures else None
                    ),
                    sundials_error_code=exc.sundials_error_code,
                    pre_charge_state_hash=getattr(backend.snapshot(), "state_hash", None),
                    attempt_failures=tuple(asdict(item) for item in exc.attempt_failures),
                    exception_type=type(exc.original_exception or exc).__name__,
                    message=exc.raw_message,
                )) from exc
            except Exception as exc:
                raise NumericalFailure(failure_context(
                    FailureReason.SOLVER_FAILURE,
                    ProtocolPhase.STANDARD_CHARGE,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                )) from exc

            if len(sequence.outcomes) != 4:
                raise NumericalFailure(failure_context(
                    FailureReason.INVALID_STATE,
                    ProtocolPhase.STANDARD_CHARGE,
                    charge_stage="capture",
                    message="standard charge sequence did not return four outcomes",
                ))
            for name, spec, outcome in zip(
                charge_names, charge_specs, sequence.outcomes, strict=True
            ):
                if not isinstance(outcome, StageOutcome) or not valid_outcome(outcome):
                    raise NumericalFailure(
                        failure_context(FailureReason.INVALID_STATE, spec.phase, outcome, charge_stage=name)
                    )
                if outcome.termination_kind is not spec.expected_termination:
                    if outcome.termination_kind in spec.allowed_physical_terminations:
                        raise PhysicalProtocolFailure(failure_context(
                            FailureReason.PHYSICAL_EVENT_BEFORE_TARGET, spec.phase, outcome,
                            charge_stage=name,
                        ))
                    reason = (
                        FailureReason.UNEXPECTED_FINAL_TIME
                        if outcome.termination_kind is TerminationKind.FINAL_TIME
                        else FailureReason.UNKNOWN_TERMINATION
                    )
                    raise NumericalFailure(failure_context(reason, spec.phase, outcome, charge_stage=name))
            durations.update(sequence.stage_durations_s)
            wall_durations.update(sequence.stage_wall_clock_durations_s)
            charge_traces = list(sequence.traces)
            solver_attempt_count = sequence.attempt_count
            solver_profile = sequence.solver_profile
            initial_solver_failure_code = sequence.initial_failure_code
            solver_attempt_failures = sequence.attempt_failures
            if self.resolved_charge_variables is not None:
                if len(charge_traces) != 4 or self.q_ref_initial_ah is None:
                    raise NumericalFailure(failure_context(
                        FailureReason.INVALID_STATE, ProtocolPhase.STANDARD_CHARGE,
                        charge_stage="capture", message="standard charge analysis did not capture four stages",
                    ))
                try:
                    charge_analysis = build_charge_analysis_bundle(
                        tuple(charge_traces), cycle=cycle, mode=self.config.mode, q_ref_ah=q_ref_ah,
                        q_ref_node=q_ref_node, q_ref_initial_ah=self.q_ref_initial_ah,
                        configured_charge_current_a=protocol.charge_3c_a,
                        nominal_capacity_ah=self.config.cell.nominal_capacity_ah,
                        faraday_constant_c_per_mol=self.config.faraday_constant_c_per_mol,
                        soc_anchor_pct=self.config.soc_anchor_pct,
                        balance_pass_limit_pct=self.config.charge_balance_pass_limit_pct,
                        balance_failure_limit_pct=self.config.charge_balance_failure_limit_pct,
                    )
                except Exception as exc:
                    if isinstance(exc, NumericalFailure):
                        raise
                    raise NumericalFailure(failure_context(
                        FailureReason.INVALID_STATE, ProtocolPhase.STANDARD_CHARGE,
                        charge_stage="analysis", exception_type=type(exc).__name__, message=str(exc),
                    )) from exc
            else:
                charge_analysis = None
            if hasattr(backend, "commit_standard_charge_sequence"):
                backend.commit_standard_charge_sequence(sequence)
            stage("post_charge_rest", StageSpec(ProtocolPhase.POST_RPT_RECOVERY, TerminationKind.FINAL_TIME), lambda spec: backend.rest(protocol.rest_after_charge_s, spec=spec))
        else:
            charge_analysis = build_skipped_charge_analysis_bundle(
                cycle=cycle, mode=self.config.mode, q_ref_ah=q_ref_ah, q_ref_node=q_ref_node
            )

        q_window_start = backend.discharge_capacity_ah()
        targets = capacity_targets(q_ref_ah, config=self.config)
        active_context_values.update(
            q_window_start_ah=q_window_start,
            step5_target_ah=targets.step5_target_ah,
            window_target_ah=targets.window_target_ah,
        )
        step5_spec = StageSpec(
            ProtocolPhase.STEP5_C4_DISCHARGE,
            TerminationKind.CAPACITY,
            allowed_physical_terminations=(TerminationKind.VOLTAGE, TerminationKind.MODEL_PHYSICAL_EVENT),
        )
        step5_outcome = stage(
                "step5_c4_discharge", step5_spec,
                lambda spec: backend.discharge_to_capacity(
                    protocol.discharge_c4_a, q_window_start, targets.step5_target_ah, self.config.cell.lower_cutoff_v
                    , spec=spec
                ),
            )
        delta_q5 = backend.discharge_capacity_ah() - q_window_start
        step5_error = abs(delta_q5 - targets.step5_target_ah) / targets.step5_target_ah
        if step5_error > self.config.capacity_window_relative_tolerance:
            raise NumericalFailure(
                failure_context(
                    FailureReason.CAPACITY_TOLERANCE_FAILURE,
                    ProtocolPhase.STEP5_C4_DISCHARGE,
                    step5_outcome,
                    q_window_start_ah=q_window_start,
                    step5_target_ah=targets.step5_target_ah,
                    step5_actual_ah=delta_q5,
                    step5_relative_error=step5_error,
                )
            )
        targets = capacity_targets(q_ref_ah, delta_q5, self.config)
        drive_window = build_drive_window_plan(
            self.base_udds,
            targets.udds_remaining_ah,
            self.config.solver.max_step_s,
            self.config,
        )
        active_context_values.update(
            step5_actual_ah=delta_q5,
            step5_relative_error=step5_error,
            actual_udds_remaining_target_ah=targets.udds_remaining_ah,
            udds_guard_ah=drive_window.guard_ah,
            udds_profile_available_ah=drive_window.profile_available_ah,
        )
        step6_spec = StageSpec(
            ProtocolPhase.STEP6_UDDS,
            TerminationKind.CAPACITY,
            allowed_physical_terminations=(TerminationKind.VOLTAGE, TerminationKind.MODEL_PHYSICAL_EVENT),
        )
        step6_outcome = stage(
                "step6_udds", step6_spec,
                lambda spec: backend.drive_cycle_to_capacity(
                    drive_window.profile,
                    q_window_start,
                    targets.window_target_ah,
                    self.config.cell.lower_cutoff_v,
                    spec=spec,
                ),
            )
        actual_window = backend.discharge_capacity_ah() - q_window_start
        target_error = abs(actual_window - targets.window_target_ah) / targets.window_target_ah
        if target_error > self.config.capacity_window_relative_tolerance:
            raise NumericalFailure(
                failure_context(
                    FailureReason.CAPACITY_TOLERANCE_FAILURE,
                    ProtocolPhase.STEP6_UDDS,
                    step6_outcome,
                    q_window_start_ah=q_window_start,
                    step5_target_ah=targets.step5_target_ah,
                    step5_actual_ah=delta_q5,
                    step5_relative_error=step5_error,
                    window_target_ah=targets.window_target_ah,
                    window_actual_ah=actual_window,
                    window_relative_error=target_error,
                    actual_udds_remaining_target_ah=targets.udds_remaining_ah,
                    udds_guard_ah=drive_window.guard_ah,
                    udds_profile_available_ah=drive_window.profile_available_ah,
                    udds_actual_ah=actual_window - delta_q5,
                )
            )
        step6_duration = step6_outcome.termination_time_s - simulated_starts["step6_udds"]
        if not step6_duration < drive_window.profile.time_s[-1]:
            raise NumericalFailure(
                failure_context(
                    FailureReason.UNEXPECTED_FINAL_TIME,
                    ProtocolPhase.STEP6_UDDS,
                    step6_outcome,
                    q_window_start_ah=q_window_start,
                    window_target_ah=targets.window_target_ah,
                    actual_udds_remaining_target_ah=targets.udds_remaining_ah,
                    udds_guard_ah=drive_window.guard_ah,
                    udds_profile_available_ah=drive_window.profile_available_ah,
                )
            )
        try:
            summary = backend.summary_metrics(started)
        except TypeError:
            summary = backend.summary_metrics()
        result = CycleResult(
            cycle=cycle,
            mode=self.config.mode,
            q_ref_ah=q_ref_ah,
            q_ref_node=q_ref_node,
            step5_target_ah=targets.step5_target_ah,
            window_target_ah=targets.window_target_ah,
            delta_q5_actual_ah=delta_q5,
            actual_udds_remaining_target_ah=targets.udds_remaining_ah,
            udds_profile_available_ah=drive_window.profile_available_ah,
            udds_guard_ah=drive_window.guard_ah,
            udds_actual_ah=actual_window - delta_q5,
            window_actual_ah=actual_window,
            start_time_s=started,
            end_time_s=backend.current_time_s(),
            stage_durations_s=durations,
            stage_wall_clock_durations_s=wall_durations,
            termination_event=step6_outcome.raw_termination or "W10_CAPACITY_WINDOW",
            termination_time_s=step6_outcome.termination_time_s,
            termination_value=step6_outcome.termination_value,
            termination_classification="EXPECTED_PROTOCOL_EVENT",
            metrics={
                **summary,
                **(precompleted_metrics or {}),
                "step5_relative_error": step5_error,
                "udds_profile_net_discharge_ah": drive_window.profile_available_ah,
                "udds_profile_target_error": abs(drive_window.profile_available_ah - (targets.udds_remaining_ah + drive_window.guard_ah)) / (targets.udds_remaining_ah + drive_window.guard_ah),
                "udds_profile_fingerprint": drive_window.profile_fingerprint,
            },
            solver_attempt_count=solver_attempt_count,
            solver_profile=solver_profile,
            initial_solver_failure_code=initial_solver_failure_code,
            solver_attempt_failures=solver_attempt_failures,
        )
        if charge_analysis is not None:
            result.charge_analysis = charge_analysis
            result.effective_charge_rate_c = protocol.charge_3c_a / q_ref_ah
            result.useful_charge_efficiency_pct = charge_analysis.summary.values.get("useful_charge_efficiency_pct")
            result.reversible_retention_pct = charge_analysis.summary.values.get("reversible_retention_pct")
            result.charge_efficiency_status = charge_analysis.status.primary_status.value
            result.complete_soc_bin_count = sum(
                row.values.get("soc_coverage_pct") == 100.0 for row in charge_analysis.soc_bins
            )
            charge_minimum = charge_analysis.summary.values.get(
                "negative_electrode_min_potential_v"
            )
            if charge_minimum is not None:
                result.metrics["negative_electrode_min_potential_v"] = float(charge_minimum)
        return result
