"""Pure, independently testable charge-efficiency calculations.

This module deliberately accepts primitive trajectories and inventory deltas. It
does not know about PyBaMM solutions, files, checkpoints, or backend state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import isfinite

import numpy as np

from .types import (
    ChargeAnalysisBundle,
    ChargeBalanceResult,
    ChargeEfficiencyAssessment,
    ChargeEfficiencyStatus,
    ChargeIntegrationResult,
    MergedChargeTrace,
    SocBoundaryCrossing,
    ChargeEfficiencySummary,
    ChargeSocBinResult,
    ChargeStageTrace,
)


def _validated_trace(time_s: Sequence[float], values: Sequence[float], label: str) -> tuple[np.ndarray, np.ndarray]:
    time = np.asarray(time_s, dtype=float)
    data = np.asarray(values, dtype=float)
    if time.ndim != 1 or data.ndim != 1 or len(time) != len(data) or len(time) < 2:
        raise ValueError(f"{label} trace must contain equally sized one-dimensional arrays with at least two points")
    if not np.all(np.isfinite(time)) or not np.all(np.isfinite(data)):
        raise ValueError(f"{label} trace contains a non-finite value")
    if np.any(np.diff(time) <= 0):
        raise ValueError(f"{label} trace time must be strictly increasing")
    return time, data


def integrate_stage_charge_ah(time_s: Sequence[float], current_a: Sequence[float]) -> float:
    """Integrate positive incoming charge from one stage only.

    PyBaMM reports charge current as negative, hence ``max(-I, 0)``.
    """
    time, current = _validated_trace(time_s, current_a, "charge")
    return float(np.trapezoid(np.maximum(-current, 0.0), time) / 3600.0)


def integrate_charge_stages(
    stages: Iterable[tuple[Sequence[float], Sequence[float]]],
    *,
    cc_stage_indexes: tuple[int, ...] = (0, 2),
    cv_stage_indexes: tuple[int, ...] = (1, 3),
) -> ChargeIntegrationResult:
    """Integrate stages locally, preventing a trapezoid across a current jump."""
    charges = tuple(integrate_stage_charge_ah(time, current) for time, current in stages)
    return ChargeIntegrationResult(
        external_charge_ah=float(sum(charges)),
        cc_charge_ah=float(sum(charges[index] for index in cc_stage_indexes if index < len(charges))),
        cv_charge_ah=float(sum(charges[index] for index in cv_stage_indexes if index < len(charges))),
    )


def reference_soc_pct(
    negative_particle_lithium_mol: Sequence[float],
    q_ref_ah: float,
    *,
    faraday_constant_c_per_mol: float = 96485.33212,
    soc_anchor_pct: float = 20.0,
) -> tuple[float, ...]:
    """Return the approved particle-lithium-delta reference SOC trajectory."""
    lithium = np.asarray(negative_particle_lithium_mol, dtype=float)
    if lithium.ndim != 1 or len(lithium) == 0 or not np.all(np.isfinite(lithium)):
        raise ValueError("negative particle lithium must be a non-empty finite vector")
    if not isfinite(q_ref_ah) or q_ref_ah <= 0 or not isfinite(faraday_constant_c_per_mol):
        raise ValueError("q_ref_ah and Faraday constant must be finite and positive")
    values = soc_anchor_pct + 100.0 * (faraday_constant_c_per_mol / 3600.0) * (lithium - lithium[0]) / q_ref_ah
    return tuple(float(value) for value in values)


def merge_charge_stage_traces(
    stages: Iterable[tuple[str, Sequence[float], Sequence[float]]],
) -> MergedChargeTrace:
    """Merge ordered charge stages without retaining a duplicate boundary row.

    A stage join belongs to the later stage, which makes the stage label and
    current value deterministic at a CC/CV discontinuity.
    """
    names: list[str] = []
    times: list[float] = []
    currents: list[float] = []
    for stage_name, stage_time, stage_current in stages:
        time, current = _validated_trace(stage_time, stage_current, stage_name)
        if times and time[0] < times[-1]:
            raise ValueError("charge stages are not chronologically ordered")
        if times and time[0] == times[-1]:
            names[-1] = stage_name
            currents[-1] = float(current[0])
            time = time[1:]
            current = current[1:]
        names.extend([stage_name] * len(time))
        times.extend(float(value) for value in time)
        currents.extend(float(value) for value in current)
    if len(times) < 2:
        raise ValueError("merged charge trace must contain at least two points")
    return MergedChargeTrace(tuple(names), tuple(times), tuple(currents))


def reference_soc_is_nonmonotonic(values: Sequence[float], *, tolerance_pct: float = 1e-6) -> bool:
    soc = np.asarray(values, dtype=float)
    if soc.ndim != 1 or len(soc) < 2 or not np.all(np.isfinite(soc)):
        raise ValueError("SOC values must be a finite one-dimensional vector")
    if tolerance_pct <= 0:
        raise ValueError("SOC monotonic tolerance must be positive")
    return bool(np.any(np.diff(soc) < -tolerance_pct))


def find_soc_boundaries(
    time_s: Sequence[float],
    soc_pct: Sequence[float],
    targets_pct: Iterable[float] = (20.0, 40.0, 60.0, 80.0, 100.0),
) -> dict[float, SocBoundaryCrossing]:
    """Find first upward SOC crossings on a discrete trace.

    Backend integration replaces this linear seed with a ``brentq`` root on the
    continuous PyBaMM solution, but the crossing count and first-up rule live
    here so they are shared by unit and integration paths.
    """
    time, soc = _validated_trace(time_s, soc_pct, "SOC")
    result: dict[float, SocBoundaryCrossing] = {}
    for target in targets_pct:
        if not isfinite(target):
            raise ValueError("SOC target must be finite")
        candidates: list[float] = []
        for index, (left, right) in enumerate(zip(soc[:-1], soc[1:], strict=True)):
            if left <= target <= right and right > left:
                fraction = (target - left) / (right - left)
                candidates.append(float(time[index] + fraction * (time[index + 1] - time[index])))
        # A target exactly at the final sample has no following bracket.
        if soc[-1] == target and (not candidates or candidates[-1] != float(time[-1])):
            candidates.append(float(time[-1]))
        result[float(target)] = SocBoundaryCrossing(
            float(target), candidates[0] if candidates else None, len(candidates)
        )
    return result


def charge_balance(
    external_charge_ah: float,
    intercalated_charge_increment_ah: float,
    reversible_plating_increment_ah: float,
    dead_lithium_increment_ah: float,
    sei_increment_ah: float,
) -> ChargeBalanceResult:
    """Calculate un-clipped efficiency and lithium-inventory charge balance."""
    values = (
        external_charge_ah,
        intercalated_charge_increment_ah,
        reversible_plating_increment_ah,
        dead_lithium_increment_ah,
        sei_increment_ah,
    )
    finite = all(isfinite(value) for value in values)
    reversible_depletion = max(-reversible_plating_increment_ah, 0.0) if isfinite(reversible_plating_increment_ah) else float("nan")
    if not finite or external_charge_ah <= 0:
        return ChargeBalanceResult(
            external_charge_ah, intercalated_charge_increment_ah, reversible_plating_increment_ah,
            reversible_depletion, dead_lithium_increment_ah, sei_increment_ah,
            None, None, None, None, None, None,
        )
    accounted = intercalated_charge_increment_ah + reversible_plating_increment_ah + dead_lithium_increment_ah + sei_increment_ah
    error = external_charge_ah - accounted
    error_pct = 100.0 * error / external_charge_ah
    return ChargeBalanceResult(
        external_charge_ah, intercalated_charge_increment_ah, reversible_plating_increment_ah,
        reversible_depletion, dead_lithium_increment_ah, sei_increment_ah,
        100.0 * intercalated_charge_increment_ah / external_charge_ah,
        100.0 * (intercalated_charge_increment_ah + reversible_plating_increment_ah) / external_charge_ah,
        accounted, error, error_pct, abs(error_pct),
    )


def assess_charge_efficiency_status(
    *,
    missing_model_variable: bool = False,
    core_failure: bool = False,
    invalid_external_charge: bool = False,
    invalid_intercalated_charge: bool = False,
    soc_anchor_invalid: bool = False,
    soc_upper_bound_not_reached: bool = False,
    non_monotonic_soc: bool = False,
    balance_abs_error_pct: float | None = None,
    balance_pass_limit_pct: float = 0.2,
    balance_failure_limit_pct: float = 1.0,
    plating_crosscheck_failed: bool = False,
    reversible_plating_increment_ah: float | None = None,
) -> ChargeEfficiencyAssessment:
    """Apply the single, stable status priority required by every output path."""
    flags: list[ChargeEfficiencyStatus] = []
    if missing_model_variable:
        flags.append(ChargeEfficiencyStatus.MISSING_MODEL_VARIABLE)
    if core_failure:
        flags.append(ChargeEfficiencyStatus.CHARGE_EFFICIENCY_CORE_FAILURE)
    if invalid_external_charge:
        flags.append(ChargeEfficiencyStatus.INVALID_EXTERNAL_CHARGE)
    if invalid_intercalated_charge:
        flags.append(ChargeEfficiencyStatus.INVALID_INTERCALATED_CHARGE)
    if soc_anchor_invalid:
        flags.append(ChargeEfficiencyStatus.SOC_ANCHOR_INVALID)
    if soc_upper_bound_not_reached:
        flags.append(ChargeEfficiencyStatus.SOC_UPPER_BOUND_NOT_REACHED)
    if non_monotonic_soc:
        flags.append(ChargeEfficiencyStatus.NON_MONOTONIC_SOC)
    if balance_abs_error_pct is not None:
        if not isfinite(balance_abs_error_pct) or balance_abs_error_pct > balance_failure_limit_pct:
            flags.append(ChargeEfficiencyStatus.CHARGE_BALANCE_FAILURE)
        elif balance_abs_error_pct > balance_pass_limit_pct:
            flags.append(ChargeEfficiencyStatus.BALANCE_WARNING)
    if plating_crosscheck_failed:
        flags.append(ChargeEfficiencyStatus.PLATING_INVENTORY_CROSSCHECK_FAILURE)
    if reversible_plating_increment_ah is not None and isfinite(reversible_plating_increment_ah) and reversible_plating_increment_ah < 0:
        flags.append(ChargeEfficiencyStatus.PREEXISTING_PLATED_LITHIUM_RELEASED)
    if not flags:
        flags.append(ChargeEfficiencyStatus.VALID)
    priority = (
        ChargeEfficiencyStatus.MISSING_MODEL_VARIABLE,
        ChargeEfficiencyStatus.CHARGE_EFFICIENCY_CORE_FAILURE,
        ChargeEfficiencyStatus.INVALID_EXTERNAL_CHARGE,
        ChargeEfficiencyStatus.INVALID_INTERCALATED_CHARGE,
        ChargeEfficiencyStatus.SOC_ANCHOR_INVALID,
        ChargeEfficiencyStatus.SOC_UPPER_BOUND_NOT_REACHED,
        ChargeEfficiencyStatus.NON_MONOTONIC_SOC,
        ChargeEfficiencyStatus.CHARGE_BALANCE_FAILURE,
        ChargeEfficiencyStatus.PLATING_INVENTORY_CROSSCHECK_FAILURE,
        ChargeEfficiencyStatus.BALANCE_WARNING,
        ChargeEfficiencyStatus.PREEXISTING_PLATED_LITHIUM_RELEASED,
        ChargeEfficiencyStatus.VALID,
    )
    primary = next(status for status in priority if status in flags)
    invalid = {
        ChargeEfficiencyStatus.MISSING_MODEL_VARIABLE,
        ChargeEfficiencyStatus.CHARGE_EFFICIENCY_CORE_FAILURE,
        ChargeEfficiencyStatus.INVALID_EXTERNAL_CHARGE,
        ChargeEfficiencyStatus.INVALID_INTERCALATED_CHARGE,
        ChargeEfficiencyStatus.SOC_ANCHOR_INVALID,
        ChargeEfficiencyStatus.SOC_UPPER_BOUND_NOT_REACHED,
        ChargeEfficiencyStatus.NON_MONOTONIC_SOC,
        ChargeEfficiencyStatus.CHARGE_BALANCE_FAILURE,
        ChargeEfficiencyStatus.PLATING_INVENTORY_CROSSCHECK_FAILURE,
    }
    return ChargeEfficiencyAssessment(primary, tuple(flags), not any(flag in invalid for flag in flags), not any(flag in invalid for flag in flags))


def _interpolate_trace_value(trace: ChargeStageTrace, key: str, time_s: float) -> float:
    values = trace.values[key]
    return float(np.interp(time_s, trace.time_s, values))


def _trace_value_at(traces: tuple[ChargeStageTrace, ...], key: str, time_s: float) -> float:
    for trace in traces:
        if trace.time_s[0] - 1e-9 <= time_s <= trace.time_s[-1] + 1e-9:
            return _interpolate_trace_value(trace, key, time_s)
    raise ValueError(f"time {time_s} is outside the charge traces")


def _interval_charge(traces: tuple[ChargeStageTrace, ...], start_time_s: float, end_time_s: float) -> ChargeIntegrationResult:
    sections: list[tuple[Sequence[float], Sequence[float]]] = []
    cc_indexes: list[int] = []
    cv_indexes: list[int] = []
    for trace in traces:
        left, right = max(start_time_s, trace.time_s[0]), min(end_time_s, trace.time_s[-1])
        if right <= left:
            continue
        time = np.asarray(trace.time_s, dtype=float)
        current = np.asarray(trace.values["current_a"], dtype=float)
        selected = time[(time > left) & (time < right)]
        points = np.concatenate(([left], selected, [right]))
        sections.append((tuple(points), tuple(np.interp(points, time, current))))
        if trace.stage_name in {"3c_cc", "c4_cc"}:
            cc_indexes.append(len(sections) - 1)
        else:
            cv_indexes.append(len(sections) - 1)
    return integrate_charge_stages(tuple(sections), cc_stage_indexes=tuple(cc_indexes), cv_stage_indexes=tuple(cv_indexes))


def _charge_window_local_traces(
    traces: tuple[ChargeStageTrace, ...],
) -> tuple[tuple[ChargeStageTrace, ...], float]:
    """Return a small, continuous charge-window time axis plus its global origin.

    Solver samples remain on their native local axes.  Only one scalar global
    origin is carried into serialization, so adding a large elapsed time cannot
    collapse distinct samples before integration or interpolation.
    """
    if not traces:
        raise ValueError("charge analysis requires at least one stage trace")
    first_time = np.asarray(traces[0].time_s, dtype=float)
    if first_time.size == 0:
        raise ValueError("charge stage trace contains no time samples")
    global_origin_s = float(traces[0].global_time_offset_s + first_time[0])
    if not np.isfinite(global_origin_s):
        raise ValueError("charge trace global time origin is not finite")

    elapsed_s = 0.0
    normalized: list[ChargeStageTrace] = []
    for trace in traces:
        local_time = np.asarray(trace.time_s, dtype=float)
        if (
            local_time.ndim != 1
            or len(local_time) < 2
            or not np.all(np.isfinite(local_time))
            or np.any(np.diff(local_time) <= 0)
        ):
            raise ValueError(f"{trace.stage_name} trace time must be strictly increasing")
        if any(len(values) != len(local_time) for values in trace.values.values()):
            raise ValueError(f"{trace.stage_name} trace values do not match its time axis")

        actual_global_start_s = float(trace.global_time_offset_s + local_time[0])
        expected_global_start_s = float(global_origin_s + elapsed_s)
        scale = max(abs(actual_global_start_s), abs(expected_global_start_s), 1.0)
        continuity_atol_s = max(1e-8, 8.0 * float(np.spacing(scale)))
        if not np.isclose(
            actual_global_start_s,
            expected_global_start_s,
            rtol=0.0,
            atol=continuity_atol_s,
        ):
            raise ValueError(f"{trace.stage_name} trace is discontinuous from the prior charge stage")

        relative_time = local_time - local_time[0]
        analysis_time = elapsed_s + relative_time
        normalized.append(ChargeStageTrace(
            trace.stage_name,
            tuple(float(value) for value in analysis_time),
            trace.values,
            global_time_offset_s=global_origin_s,
        ))
        elapsed_s = float(analysis_time[-1])
    return tuple(normalized), global_origin_s


def build_charge_analysis_bundle(
    stage_traces: tuple[ChargeStageTrace, ...],
    *,
    cycle: int,
    mode: str,
    q_ref_ah: float,
    q_ref_node: int,
    q_ref_initial_ah: float,
    configured_charge_current_a: float,
    nominal_capacity_ah: float,
    faraday_constant_c_per_mol: float = 96485.33212,
    soc_anchor_pct: float = 20.0,
    balance_pass_limit_pct: float = 0.2,
    balance_failure_limit_pct: float = 1.0,
) -> ChargeAnalysisBundle:
    """Build an auditable full-window and four-bin charge analysis from traces.

    The backend supplies stage-local samples; this function owns all accounting
    and has no filesystem or solver dependency.
    """
    if tuple(trace.stage_name for trace in stage_traces) != ("3c_cc", "4v_cv", "c4_cc", "4p2v_cv"):
        raise ValueError("charge analysis requires the four standard charge stages in protocol order")
    required = {
        "current_a", "terminal_voltage_v", "temperature_k", "negative_particle_lithium_mol",
        "total_plating_inventory_ah", "dead_lithium_inventory_ah", "reversible_plating_inventory_ah",
        "cumulative_sei_loss_ah", "negative_electrode_surface_potential_difference_v",
    }
    if any(not required <= set(trace.values) for trace in stage_traces):
        raise ValueError("charge trace is missing a core accounting variable")
    stage_traces, global_time_origin_s = _charge_window_local_traces(stage_traces)

    def global_time(local_time_s: float) -> float:
        return float(global_time_origin_s + local_time_s)

    times: list[float] = []
    lithium: list[float] = []
    stages: list[str] = []
    currents: list[float] = []
    for trace in stage_traces:
        for index, time in enumerate(trace.time_s):
            if times and time == times[-1]:
                times[-1] = time
                stages[-1] = trace.stage_name
                currents[-1] = trace.values["current_a"][index]
                lithium[-1] = trace.values["negative_particle_lithium_mol"][index]
            else:
                times.append(time)
                stages.append(trace.stage_name)
                currents.append(trace.values["current_a"][index])
                lithium.append(trace.values["negative_particle_lithium_mol"][index])
    soc = reference_soc_pct(lithium, q_ref_ah, faraday_constant_c_per_mol=faraday_constant_c_per_mol, soc_anchor_pct=soc_anchor_pct)
    boundaries = find_soc_boundaries(times, soc)
    nonmonotonic = reference_soc_is_nonmonotonic(soc)
    whole = _interval_charge(stage_traces, times[0], times[-1])
    start = {key: _trace_value_at(stage_traces, key, times[0]) for key in required if key != "current_a"}
    end = {key: _trace_value_at(stage_traces, key, times[-1]) for key in required if key != "current_a"}
    balance = charge_balance(
        whole.external_charge_ah,
        faraday_constant_c_per_mol * (end["negative_particle_lithium_mol"] - start["negative_particle_lithium_mol"]) / 3600.0,
        end["reversible_plating_inventory_ah"] - start["reversible_plating_inventory_ah"],
        end["dead_lithium_inventory_ah"] - start["dead_lithium_inventory_ah"],
        end["cumulative_sei_loss_ah"] - start["cumulative_sei_loss_ah"],
    )
    assessment = assess_charge_efficiency_status(
        invalid_external_charge=balance.useful_charge_efficiency_pct is None,
        invalid_intercalated_charge=not isfinite(balance.intercalated_charge_increment_ah) or balance.intercalated_charge_increment_ah < 0,
        soc_upper_bound_not_reached=boundaries[100.0].time_s is None,
        non_monotonic_soc=nonmonotonic,
        balance_abs_error_pct=balance.charge_balance_abs_error_pct,
        balance_pass_limit_pct=balance_pass_limit_pct,
        balance_failure_limit_pct=balance_failure_limit_pct,
        reversible_plating_increment_ah=balance.reversible_plating_increment_ah,
    )
    boundary_100 = boundaries[100.0].time_s
    post_100 = _interval_charge(stage_traces, boundary_100, times[-1]) if boundary_100 is not None and boundary_100 < times[-1] else ChargeIntegrationResult(0.0, 0.0, 0.0)
    summary_values = {
        "configured_charge_current_a": configured_charge_current_a,
        "configured_nominal_charge_rate_c": configured_charge_current_a / nominal_capacity_ah,
        "effective_charge_rate_c": configured_charge_current_a / q_ref_ah,
        "nominal_capacity_ah": nominal_capacity_ah, "q_ref_ah": q_ref_ah, "q_ref_node": q_ref_node,
        "soh_pct": 100.0 * q_ref_ah / q_ref_initial_ah, "soc_start_pct": soc[0], "soc_at_charge_end_pct": soc[-1],
        "soc_definition": "NEGATIVE_PARTICLE_LITHIUM_DELTA_OVER_FROZEN_Q_REF_V1", "soc_reference_capacity_ah": q_ref_ah,
        "capacity_reference_node": q_ref_node, "soc_anchor_pct": soc_anchor_pct, "soc_anchor_source": "INITIAL_SOC_CONFIGURATION" if cycle == 1 else "W10_80_PERCENT_DISCHARGE_WINDOW",
        "soc_anchor_validation_status": "VALID", "time_start_s": global_time(times[0]), "time_end_s": global_time(times[-1]), "duration_s": times[-1] - times[0],
        "post_100_charge_ah": post_100.external_charge_ah, "post_100_duration_s": 0.0 if boundary_100 is None else max(0.0, times[-1] - boundary_100),
        "external_charge_ah": whole.external_charge_ah, "cc_charge_ah": whole.cc_charge_ah, "cv_charge_ah": whole.cv_charge_ah,
        "cv_charge_fraction_pct": 100.0 * whole.cv_charge_ah / whole.external_charge_ah if whole.external_charge_ah else None,
        "negative_particle_lithium_mol_start": start["negative_particle_lithium_mol"], "negative_particle_lithium_mol_end": end["negative_particle_lithium_mol"],
        "faraday_constant_c_per_mol": faraday_constant_c_per_mol, "intercalated_charge_increment_ah": balance.intercalated_charge_increment_ah,
        "total_plating_inventory_start_ah": start["total_plating_inventory_ah"], "total_plating_inventory_end_ah": end["total_plating_inventory_ah"],
        "reversible_plating_inventory_start_ah": start["reversible_plating_inventory_ah"], "reversible_plating_inventory_end_ah": end["reversible_plating_inventory_ah"],
        "reversible_plating_increment_ah": balance.reversible_plating_increment_ah, "reversible_plating_depletion_ah": balance.reversible_plating_depletion_ah,
        "dead_lithium_inventory_start_ah": start["dead_lithium_inventory_ah"], "dead_lithium_inventory_end_ah": end["dead_lithium_inventory_ah"], "dead_lithium_increment_ah": balance.dead_lithium_increment_ah,
        "sei_inventory_start_ah": start["cumulative_sei_loss_ah"], "sei_inventory_end_ah": end["cumulative_sei_loss_ah"], "sei_increment_ah": balance.sei_increment_ah,
        "useful_charge_efficiency_pct": balance.useful_charge_efficiency_pct, "reversible_retention_pct": balance.reversible_retention_pct,
        "accounted_charge_ah": balance.accounted_charge_ah, "charge_balance_error_ah": balance.charge_balance_error_ah,
        "charge_balance_error_pct": balance.charge_balance_error_pct, "charge_balance_abs_error_pct": balance.charge_balance_abs_error_pct,
        "charge_balance_status": assessment.primary_status.value, "charge_integration_method": "STAGE_LOCAL_TRAPEZOID_WITH_EXACT_SOC_BOUNDARIES_V1",
        "charge_integration_point_count": len(times), "primary_status": assessment.primary_status, "status_flags": assessment.status_flags,
        "is_valid_for_efficiency_analysis": assessment.is_valid_for_efficiency_analysis, "is_valid_for_mechanism_analysis": assessment.is_valid_for_mechanism_analysis,
        "negative_electrode_min_potential_v": min(
            min(trace.values["negative_electrode_surface_potential_difference_v"])
            for trace in stage_traces
        ),
    }
    bins: list[ChargeSocBinResult] = []
    for lower, upper in zip((20.0, 40.0, 60.0, 80.0), (40.0, 60.0, 80.0, 100.0), strict=True):
        start_time, end_time = boundaries[lower].time_s, boundaries[upper].time_s
        values = {"soc_start_pct": lower, "soc_end_pct": upper, "q_ref_ah": q_ref_ah, "q_ref_node": q_ref_node,
                  "configured_charge_current_a": configured_charge_current_a, "configured_nominal_charge_rate_c": configured_charge_current_a / nominal_capacity_ah,
                  "effective_charge_rate_c": configured_charge_current_a / q_ref_ah, "nominal_capacity_ah": nominal_capacity_ah,
                  "soh_pct": 100.0 * q_ref_ah / q_ref_initial_ah, "soc_definition": summary_values["soc_definition"],
                  "soc_reference_capacity_ah": q_ref_ah, "capacity_reference_node": q_ref_node, "soc_anchor_pct": soc_anchor_pct,
                  "soc_anchor_source": summary_values["soc_anchor_source"], "soc_crossing_count": boundaries[upper].crossing_count,
                  "soc_crossing_selection_rule": boundaries[upper].selection_rule, "primary_status": assessment.primary_status,
                  "status_flags": assessment.status_flags, "is_valid_for_efficiency_analysis": assessment.is_valid_for_efficiency_analysis,
                  "is_valid_for_mechanism_analysis": assessment.is_valid_for_mechanism_analysis}
        if start_time is not None and end_time is not None:
            part = _interval_charge(stage_traces, start_time, end_time)
            beginning = {key: _trace_value_at(stage_traces, key, start_time) for key in required if key != "current_a"}
            finishing = {key: _trace_value_at(stage_traces, key, end_time) for key in required if key != "current_a"}
            part_balance = charge_balance(part.external_charge_ah, faraday_constant_c_per_mol * (finishing["negative_particle_lithium_mol"] - beginning["negative_particle_lithium_mol"]) / 3600.0, finishing["reversible_plating_inventory_ah"] - beginning["reversible_plating_inventory_ah"], finishing["dead_lithium_inventory_ah"] - beginning["dead_lithium_inventory_ah"], finishing["cumulative_sei_loss_ah"] - beginning["cumulative_sei_loss_ah"])
            values.update({"actual_soc_start_pct": lower, "actual_soc_end_pct": upper, "soc_coverage_pct": 100.0, "time_start_s": global_time(start_time), "time_end_s": global_time(end_time), "duration_s": end_time - start_time, "external_charge_ah": part.external_charge_ah, "cc_charge_ah": part.cc_charge_ah, "cv_charge_ah": part.cv_charge_ah, "cv_charge_fraction_pct": 100.0 * part.cv_charge_ah / part.external_charge_ah if part.external_charge_ah else None, "negative_particle_lithium_mol_start": beginning["negative_particle_lithium_mol"], "negative_particle_lithium_mol_end": finishing["negative_particle_lithium_mol"], "intercalated_charge_increment_ah": part_balance.intercalated_charge_increment_ah, "reversible_plating_increment_ah": part_balance.reversible_plating_increment_ah, "reversible_plating_depletion_ah": part_balance.reversible_plating_depletion_ah, "dead_lithium_increment_ah": part_balance.dead_lithium_increment_ah, "sei_increment_ah": part_balance.sei_increment_ah, "useful_charge_efficiency_pct": part_balance.useful_charge_efficiency_pct, "reversible_retention_pct": part_balance.reversible_retention_pct, "charge_balance_error_ah": part_balance.charge_balance_error_ah, "charge_balance_error_pct": part_balance.charge_balance_error_pct, "charge_balance_abs_error_pct": part_balance.charge_balance_abs_error_pct, "charge_balance_status": assessment.primary_status.value})
        else:
            values.update({"actual_soc_start_pct": lower if start_time is not None else "", "actual_soc_end_pct": "", "soc_coverage_pct": 0.0})
        bins.append(ChargeSocBinResult(cycle, mode, f"{int(lower)}-{int(upper)}", values))
    trace_times = sorted({*times, *(crossing.time_s for crossing in boundaries.values() if crossing.time_s is not None)})
    trace_rows = []
    for time in trace_times:
        stage = next(trace.stage_name for trace in reversed(stage_traces) if trace.time_s[0] - 1e-9 <= time <= trace.time_s[-1] + 1e-9)
        lithium_at_time = _trace_value_at(stage_traces, "negative_particle_lithium_mol", time)
        row = {
            "cycle": cycle, "charge_stage": stage, "time_s": global_time(time),
            "current_a": _trace_value_at(stage_traces, "current_a", time),
            "terminal_voltage_v": _trace_value_at(stage_traces, "terminal_voltage_v", time),
            "temperature_k": _trace_value_at(stage_traces, "temperature_k", time),
            "reference_soc_pct": soc_anchor_pct + 100.0 * faraday_constant_c_per_mol * (lithium_at_time - lithium[0]) / (3600.0 * q_ref_ah),
            "cumulative_external_charge_ah": _interval_charge(stage_traces, times[0], time).external_charge_ah if time > times[0] else 0.0,
            "negative_particle_lithium_mol": lithium_at_time,
            "total_plating_inventory_ah": _trace_value_at(stage_traces, "total_plating_inventory_ah", time),
            "dead_lithium_inventory_ah": _trace_value_at(stage_traces, "dead_lithium_inventory_ah", time),
            "reversible_plating_inventory_ah": _trace_value_at(stage_traces, "reversible_plating_inventory_ah", time),
            "cumulative_sei_loss_ah": _trace_value_at(stage_traces, "cumulative_sei_loss_ah", time),
            "soc_boundary": next((target for target, crossing in boundaries.items() if crossing.time_s is not None and abs(crossing.time_s - time) < 1e-9), ""),
        }
        if trace_rows and float(row["time_s"]) == float(trace_rows[-1]["time_s"]):
            trace_rows[-1] = row
        elif trace_rows and float(row["time_s"]) < float(trace_rows[-1]["time_s"]):
            raise ValueError("charge trace global output time moved backwards")
        else:
            trace_rows.append(row)
    trace_rows = tuple(trace_rows)
    return ChargeAnalysisBundle(ChargeEfficiencySummary(cycle, mode, summary_values), tuple(bins), assessment, trace_rows)


def build_skipped_charge_analysis_bundle(
    *, cycle: int, mode: str, q_ref_ah: float, q_ref_node: int
) -> ChargeAnalysisBundle:
    """Represent RPT recovery's already-complete charge without inventing data."""
    assessment = ChargeEfficiencyAssessment(
        ChargeEfficiencyStatus.STANDARD_CHARGE_SKIPPED_AFTER_RPT,
        (ChargeEfficiencyStatus.STANDARD_CHARGE_SKIPPED_AFTER_RPT,), False, False,
    )
    common = {
        "q_ref_ah": q_ref_ah, "q_ref_node": q_ref_node,
        "primary_status": assessment.primary_status, "status_flags": assessment.status_flags,
        "is_valid_for_efficiency_analysis": False, "is_valid_for_mechanism_analysis": False,
    }
    summary = ChargeEfficiencySummary(cycle, mode, common)
    bins = tuple(ChargeSocBinResult(cycle, mode, name, {**common, "soc_start_pct": lower, "soc_end_pct": upper, "soc_coverage_pct": 0.0})
                 for name, lower, upper in (("20-40", 20.0, 40.0), ("40-60", 40.0, 60.0), ("60-80", 60.0, 80.0), ("80-100", 80.0, 100.0)))
    return ChargeAnalysisBundle(summary, bins, assessment)
