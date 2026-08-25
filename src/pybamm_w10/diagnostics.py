"""Capacity RPT definitions and stable placeholders for future diagnostics."""

from __future__ import annotations

import copy
from dataclasses import replace
from hashlib import sha256
from typing import Callable, Protocol, runtime_checkable

import numpy as np

from .config import RunConfig
from .types import (
    CapacityTargets,
    FailureContext,
    FailureReason,
    NumericalFailure,
    PhysicalProtocolFailure,
    ProtocolPhase,
    RPTResult,
    StageOutcome,
    StageSpec,
    TerminationKind,
)


@runtime_checkable
class DiagnosticBackend(Protocol):
    """Adapter used by RPT orchestration; production backend owns PyBaMM calls."""

    def snapshot(self): ...
    def fork(self): ...
    def current_time_s(self) -> float: ...
    def cc_charge_to_voltage(self, current_a: float, voltage_v: float, *, spec: StageSpec) -> StageOutcome: ...
    def cv_hold_to_current(self, voltage_v: float, cutoff_current_a: float, *, spec: StageSpec) -> StageOutcome: ...
    def rest(self, duration_s: float, *, spec: StageSpec) -> StageOutcome: ...
    def discharge_to_voltage(self, current_a: float, voltage_v: float, *, spec: StageSpec) -> StageOutcome: ...
    def discharge_capacity_ah(self) -> float: ...
    def timeseries_since(self, start_time_s: float = 0.0): ...


def capacity_targets(q_ref_ah: float, delta_q5_actual_ah: float = 0.0, config: RunConfig | None = None) -> CapacityTargets:
    protocol = (config or RunConfig()).protocol
    if not np.isfinite(q_ref_ah) or q_ref_ah <= 0:
        raise ValueError("Q_ref must be finite and positive")
    step5 = protocol.capacity_fraction_step5 * q_ref_ah
    window = protocol.capacity_fraction_window * q_ref_ah
    remaining = window - delta_q5_actual_ah
    if remaining <= 0:
        raise ValueError("Step 5 already consumed the full 80% protocol window")
    return CapacityTargets(q_ref_ah, step5, window, remaining)


def run_validated_stage(
    call: Callable[[StageSpec], StageOutcome],
    spec: StageSpec,
    *,
    mode: str,
    rpt_node: int,
) -> StageOutcome:
    """Execute and validate one RPT or RPT-recovery stage.

    RPT calls use the same termination contract as aging-cycle stages: a
    completed solve is not sufficient unless it ended at the stage's declared
    boundary with finite terminal evidence.
    """
    try:
        outcome = call(spec)
    except (PhysicalProtocolFailure, NumericalFailure):
        raise
    except Exception as exc:
        raise NumericalFailure(
            FailureContext(
                reason=FailureReason.SOLVER_FAILURE,
                mode=mode,
                rpt_node=rpt_node,
                phase=spec.phase,
                exception_type=type(exc).__name__,
                message=str(exc),
            )
        ) from exc

    if not isinstance(outcome, StageOutcome):
        raise NumericalFailure(
            FailureContext(
                reason=FailureReason.INVALID_STATE,
                mode=mode,
                rpt_node=rpt_node,
                phase=spec.phase,
                message="RPT stage did not return a StageOutcome",
            )
        )
    values = (
        outcome.termination_time_s,
        outcome.terminal_voltage_v,
        outcome.terminal_temperature_k,
        outcome.terminal_discharge_capacity_ah,
    )
    context = FailureContext(
        reason=FailureReason.INVALID_STATE,
        mode=mode,
        rpt_node=rpt_node,
        phase=spec.phase,
        termination_kind=outcome.termination_kind,
        raw_termination=outcome.raw_termination,
        termination_time_s=outcome.termination_time_s,
        termination_value=outcome.termination_value,
        terminal_voltage_v=outcome.terminal_voltage_v,
        terminal_temperature_k=outcome.terminal_temperature_k,
        terminal_discharge_capacity_ah=outcome.terminal_discharge_capacity_ah,
        state_hash=outcome.state_hash,
    )
    if not outcome.state_hash or any(value is None or not np.isfinite(value) for value in values):
        raise NumericalFailure(context)
    if outcome.termination_kind is spec.expected_termination:
        return outcome
    if outcome.termination_kind in spec.allowed_physical_terminations:
        raise PhysicalProtocolFailure(
            replace(context, reason=FailureReason.PHYSICAL_EVENT_BEFORE_TARGET)
        )
    if outcome.termination_kind is TerminationKind.FINAL_TIME:
        raise NumericalFailure(replace(context, reason=FailureReason.UNEXPECTED_FINAL_TIME))
    raise NumericalFailure(replace(context, reason=FailureReason.UNKNOWN_TERMINATION))


def run_capacity_rpt(
    backend: DiagnosticBackend,
    node: int,
    config: RunConfig,
    initial_capacity_ah: float | None,
    *,
    virtual: bool,
) -> RPTResult:
    """Run the specified 1C/CV/rest/0.24A RPT and measure its local Ah increment."""
    main_before = backend.snapshot()
    main_time_before = backend.current_time_s()
    main_capacity_before = backend.discharge_capacity_ah()
    diagnostic = (
        backend.fork()
        if virtual and hasattr(backend, "fork")
        else copy.deepcopy(backend)
        if virtual
        else backend
    )
    start_time = diagnostic.current_time_s()
    preconditioning = StageSpec(
        ProtocolPhase.RPT_PRECONDITIONING,
        TerminationKind.VOLTAGE,
        allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
    )
    run_validated_stage(
        lambda spec: diagnostic.cc_charge_to_voltage(
            config.cell.nominal_capacity_ah, config.cell.upper_cutoff_v, spec=spec
        ),
        preconditioning,
        mode=config.mode,
        rpt_node=node,
    )
    run_validated_stage(
        lambda spec: diagnostic.cv_hold_to_current(
            config.cell.upper_cutoff_v, config.protocol.cv_cutoff_a, spec=spec
        ),
        StageSpec(
            ProtocolPhase.RPT_PRECONDITIONING,
            TerminationKind.CURRENT,
            allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
        ),
        mode=config.mode,
        rpt_node=node,
    )
    run_validated_stage(
        lambda spec: diagnostic.rest(config.protocol.rpt_rest_s, spec=spec),
        StageSpec(
            ProtocolPhase.RPT_PRECONDITIONING,
            TerminationKind.FINAL_TIME,
            allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
        ),
        mode=config.mode,
        rpt_node=node,
    )
    q_start = diagnostic.discharge_capacity_ah()
    run_validated_stage(
        lambda spec: diagnostic.discharge_to_voltage(
            config.protocol.rpt_discharge_a, config.cell.lower_cutoff_v, spec=spec
        ),
        StageSpec(
            ProtocolPhase.RPT_CAPACITY_DISCHARGE,
            TerminationKind.VOLTAGE,
            allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
        ),
        mode=config.mode,
        rpt_node=node,
    )
    q_end = diagnostic.discharge_capacity_ah()
    end_time = diagnostic.current_time_s()
    capacity = q_end - q_start
    if not np.isfinite(capacity) or capacity <= 0:
        raise ValueError(f"RPT at node {node} returned invalid local capacity {capacity!r}")
    diagnostic_trace = (
        diagnostic.timeseries_since(start_time)
        if hasattr(diagnostic, "timeseries_since")
        else {}
    )
    main_after = backend.snapshot()
    main_time_after = backend.current_time_s()
    main_capacity_after = backend.discharge_capacity_ah()
    changed = main_after != main_before
    if virtual and changed:
        raise RuntimeError("virtual RPT changed the main backend state")
    nominal_soh = 100 * capacity / config.cell.nominal_capacity_ah
    initial_soh = 100.0 if node == 0 and initial_capacity_ah is None else (
        None if initial_capacity_ah is None else 100 * capacity / initial_capacity_ah
    )
    return RPTResult(
        node=node,
        q_rpt_start_ah=q_start,
        q_rpt_end_ah=q_end,
        capacity_ah=capacity,
        soh_initial_pct=initial_soh,
        soh_nominal_pct=nominal_soh,
        mode=config.mode,
        start_time_s=start_time,
        end_time_s=end_time,
        diagnostic_duration_s=end_time - start_time,
        changed_main_state=changed,
        main_state_hash_before=getattr(
            main_before, "state_hash", sha256(repr(main_before).encode()).hexdigest()
        ),
        main_state_hash_after=getattr(
            main_after, "state_hash", sha256(repr(main_after).encode()).hexdigest()
        ),
        main_time_before_s=main_time_before,
        main_time_after_s=main_time_after,
        main_capacity_before_ah=main_capacity_before,
        main_capacity_after_ah=main_capacity_after,
        became_q_ref=node < config.protocol.max_aging_cycles,
        targets=capacity_targets(capacity, config=config) if node < config.protocol.max_aging_cycles else None,
        timeseries=diagnostic_trace,
    )


def run_hppc(*args, **kwargs):
    raise NotImplementedError("HPPC physics is intentionally outside this W10 implementation")


def run_eis(*args, **kwargs):
    raise NotImplementedError("EIS physics is intentionally outside this W10 implementation")
