"""Identify, average, and validate the repeated W10 Step-14 drive cycle."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping

import numpy as np

from .config import RunConfig
from .types import DriveWindowPlan


@dataclass(frozen=True)
class CurrentProfile:
    time_s: np.ndarray
    current_a: np.ndarray

    def __post_init__(self) -> None:
        if len(self.time_s) != len(self.current_a) or len(self.time_s) < 2:
            raise ValueError("current profile requires matching arrays with at least two points")
        if not np.all(np.isfinite(self.time_s)) or not np.all(np.isfinite(self.current_a)):
            raise ValueError("current profile contains NaN or Inf")
        if not np.all(np.diff(self.time_s) > 0):
            raise ValueError("current-profile time must be strictly increasing")

    @property
    def net_discharge_ah(self) -> float:
        return float(np.trapezoid(self.current_a, self.time_s) / 3600)

    @property
    def recharge_ah(self) -> float:
        return float(-np.trapezoid(np.minimum(self.current_a, 0), self.time_s) / 3600)

    @property
    def rms_a(self) -> float:
        return float(np.sqrt(np.mean(self.current_a**2)))

    @property
    def fingerprint(self) -> str:
        digest = sha256()
        digest.update(np.ascontiguousarray(self.time_s).tobytes())
        digest.update(np.ascontiguousarray(self.current_a).tobytes())
        return digest.hexdigest()


@dataclass(frozen=True)
class WaveformValidation:
    period_s: int
    segment_count: int
    complete_unit_count: int
    source_rms_a: float
    profile_rms_a: float
    source_min_a: float
    source_max_a: float
    profile_min_a: float
    profile_max_a: float
    source_recharge_ah: float
    profile_recharge_ah: float
    source_net_discharge_ah: float
    profile_net_discharge_ah: float

    def to_json(self) -> dict[str, float | int]:
        return self.__dict__.copy()


def _field(data: Mapping[str, object], needle: str) -> np.ndarray:
    matches = [key for key in data if needle.lower() in key.lower()]
    if len(matches) != 1:
        raise KeyError(f"expected one MAT field containing {needle!r}; found {matches}")
    return np.asarray(data[matches[0]], dtype=float).squeeze()


def load_step14_segments(path: Path, step: int = 14) -> list[CurrentProfile]:
    """Return every continuous Step-14 occurrence using the PyBaMM current sign."""
    from scipy.io import loadmat

    raw = loadmat(path, squeeze_me=True)
    current_raw = _field(raw, "I_full_vec")
    step_index = _field(raw, "Step_Index_full_vec")
    time_s = _field(raw, "t_full_vec")
    mask = np.asarray(step_index == step)
    if not np.any(mask):
        raise ValueError(f"W10 data contains no samples for Step {step}")
    time = time_s[mask]
    current = -current_raw[mask]
    order = np.argsort(time, kind="stable")
    time, current = time[order], current[order]
    unique, indices = np.unique(time, return_index=True)
    if len(unique) != len(time):
        last_indices = np.r_[indices[1:] - 1, len(time) - 1]
        time, current = time[last_indices], current[last_indices]
    delta = np.diff(time)
    positive = delta[delta > 0]
    if not len(positive):
        raise ValueError("Step 14 has no increasing acquisition timestamps")
    gap_limit = 10 * np.median(positive)
    splits = np.flatnonzero(delta > gap_limit)
    starts = np.r_[0, splits + 1]
    ends = np.r_[splits, len(time) - 1]
    segments = [
        CurrentProfile(time[start : end + 1] - time[start], current[start : end + 1])
        for start, end in zip(starts, ends, strict=True)
        if end > start
    ]
    if not segments:
        raise ValueError("W10 Step 14 contains no continuous segment")
    return segments


def load_step14_from_mat(path: Path, step: int = 14) -> CurrentProfile:
    """Compatibility helper returning the longest continuous Step-14 segment."""
    return max(load_step14_segments(path, step), key=lambda item: item.time_s[-1])


def _period_metrics(profile: CurrentProfile, period_s: int) -> tuple[float, float]:
    grid = np.arange(0.0, np.floor(profile.time_s[-1]) + 1.0, 1.0)
    values = np.interp(grid, profile.time_s, profile.current_a)
    if len(values) <= period_s + 1:
        raise ValueError("segment is too short for candidate period")
    first, shifted = values[:-period_s], values[period_s:]
    correlation = float(np.corrcoef(first, shifted)[0, 1])
    scale = max(float(np.sqrt(np.mean(first**2))), np.finfo(float).eps)
    nrmse = float(np.sqrt(np.mean((first - shifted) ** 2)) / scale)
    return correlation, nrmse


def identify_repeat_period(
    segments: list[CurrentProfile],
    candidate_min_s: int = 2400,
    candidate_max_s: int = 2800,
) -> tuple[int, dict[str, object]]:
    """Require every valid W10 segment to select one common integer-second period."""
    if candidate_min_s < 2 or candidate_max_s < candidate_min_s:
        raise ValueError("invalid period candidate range")
    candidates = range(candidate_min_s, candidate_max_s + 1)
    segment_scores: list[dict[int, tuple[float, float]]] = []
    individual_best: list[int] = []
    for segment in segments:
        scores = {candidate: _period_metrics(segment, candidate) for candidate in candidates}
        segment_scores.append(scores)
        individual_best.append(min(scores, key=lambda key: (scores[key][1], -scores[key][0])))
    if len(set(individual_best)) != 1:
        raise ValueError(f"W10 Step-14 segments disagree on repeat period: {individual_best}")
    period = individual_best[0]
    candidate_summary = []
    for candidate in candidates:
        correlations = [scores[candidate][0] for scores in segment_scores]
        errors = [scores[candidate][1] for scores in segment_scores]
        candidate_summary.append(
            {
                "period_s": candidate,
                "mean_correlation": float(np.mean(correlations)),
                "mean_normalized_rmse": float(np.mean(errors)),
            }
        )
    correlations = [scores[period][0] for scores in segment_scores]
    errors = [scores[period][1] for scores in segment_scores]
    divisor_evidence = {}
    for divisor in range(2, period + 1):
        if period % divisor == 0 and period // divisor >= 200:
            candidate = period // divisor
            divisor_evidence[str(candidate)] = [
                {"correlation": corr, "normalized_rmse": error}
                for corr, error in (_period_metrics(segment, candidate) for segment in segments)
            ]
    return period, {
        "candidate_range_s": [candidate_min_s, candidate_max_s],
        "segment_best_periods_s": individual_best,
        "selected_period_s": period,
        "selected_correlation_range": [min(correlations), max(correlations)],
        "selected_normalized_rmse_range": [min(errors), max(errors)],
        "candidate_scores": candidate_summary,
        "divisor_exclusion": divisor_evidence,
    }


def _complete_units(segments: list[CurrentProfile], period_s: int) -> list[np.ndarray]:
    phase = np.arange(0.0, float(period_s) + 1.0, 1.0)
    units: list[np.ndarray] = []
    for segment in segments:
        count = int(segment.time_s[-1] // period_s)
        for unit in range(count):
            start = unit * period_s
            units.append(np.interp(start + phase, segment.time_s, segment.current_a))
    if not units:
        raise ValueError("no complete W10 repeat units available")
    return units


def phase_average_segments_1hz(
    segments: list[CurrentProfile], period_s: int
) -> tuple[CurrentProfile, list[CurrentProfile]]:
    phase = np.arange(0.0, float(period_s) + 1.0, 1.0)
    arrays = _complete_units(segments, period_s)
    units = [CurrentProfile(phase, values) for values in arrays]
    return CurrentProfile(phase, np.mean(np.stack(arrays), axis=0)), units


def phase_average_1hz(profile: CurrentProfile, period_s: int = 2600) -> CurrentProfile:
    return phase_average_segments_1hz([profile], period_s)[0]


def _solve_segment_crossing(i0: float, i1: float, dt: float, remaining_ah: float) -> float:
    slope = (i1 - i0) / dt
    c = -3600.0 * remaining_ah
    if abs(slope) < 1e-14:
        if i0 <= 0:
            raise RuntimeError("target crossing cannot occur in a non-discharge segment")
        return -c / i0
    discriminant = i0 * i0 - 2.0 * slope * c
    if discriminant < 0:
        raise RuntimeError("capacity crossing has no real root")
    root = np.sqrt(discriminant)
    candidates = [(-i0 + root) / slope, (-i0 - root) / slope]
    valid = [value for value in candidates if -1e-10 <= value <= dt + 1e-10]
    if not valid:
        raise RuntimeError("capacity crossing root lies outside the sample interval")
    return min(max(0.0, value) for value in valid)


def repeat_to_net_discharge(base: CurrentProfile, target_ah: float) -> CurrentProfile:
    """Repeat a waveform and analytically truncate the first crossing of target Ah."""
    if not np.isfinite(target_ah) or target_ah <= 0:
        raise ValueError("UDDS net-discharge target must be finite and positive")
    if base.net_discharge_ah <= 0:
        raise ValueError("base waveform must have positive net discharge")
    repeats = max(1, int(np.ceil(target_ah / base.net_discharge_ah)) + 2)
    period = base.time_s[-1]
    time = np.concatenate([base.time_s + repeat * period for repeat in range(repeats)])
    current = np.tile(base.current_a, repeats)
    keep = np.r_[True, np.diff(time) > 0]
    time, current = time[keep], current[keep]
    cumulative = 0.0
    for index in range(len(time) - 1):
        dt = float(time[index + 1] - time[index])
        i0, i1 = float(current[index]), float(current[index + 1])
        slope = (i1 - i0) / dt
        probes = [0.0, dt]
        if slope and 0.0 < -i0 / slope < dt:
            probes.append(-i0 / slope)
        maximum = max(cumulative + (i0 * value + 0.5 * slope * value**2) / 3600 for value in probes)
        if maximum >= target_ah - 1e-14:
            crossing = _solve_segment_crossing(i0, i1, dt, target_ah - cumulative)
            endpoint_t = time[index] + crossing
            endpoint_i = i0 + slope * crossing
            return CurrentProfile(
                np.r_[time[: index + 1], endpoint_t],
                np.r_[current[: index + 1], endpoint_i],
            )
        cumulative += (i0 + i1) * dt / 7200
    raise RuntimeError("insufficient repeated UDDS duration to reach target")


def build_drive_window_plan(
    base: CurrentProfile,
    remaining_ah: float,
    max_step_s: float,
    config: RunConfig,
) -> DriveWindowPlan:
    """Build the only permitted Step-6 profile/event relationship.

    The capacity event remains at ``remaining_ah``.  The extra profile
    capacity is a solver-domain guard and is never part of the event target or
    reported cycle capacity.
    """
    if not np.isfinite(remaining_ah) or remaining_ah <= 0:
        raise ValueError("UDDS remaining target must be finite and positive")
    if not np.isfinite(max_step_s) or max_step_s <= 0:
        raise ValueError("solver maximum step must be finite and positive")
    if base.net_discharge_ah <= 0:
        raise ValueError("base waveform must have positive net discharge")

    max_abs_current_a = float(np.max(np.abs(base.current_a)))
    if not np.isfinite(max_abs_current_a) or max_abs_current_a <= 0:
        raise ValueError("base waveform must have a finite non-zero current")
    relative_guard = config.udds_event_guard_fraction * remaining_ah
    solver_guard = (
        config.udds_event_guard_solver_steps * max_abs_current_a * max_step_s / 3600
    )
    guard_ah = max(relative_guard, solver_guard)
    available_ah = remaining_ah + guard_ah
    profile = repeat_to_net_discharge(base, available_ah)
    construction_error = abs(profile.net_discharge_ah - available_ah)
    tolerance = max(1e-12, available_ah * 1e-10)
    if construction_error > tolerance:
        raise ValueError(
            f"UDDS profile construction error {construction_error:.3g} exceeds {tolerance:.3g}"
        )
    if profile.net_discharge_ah <= remaining_ah:
        raise ValueError("UDDS profile capacity must strictly exceed capacity-event target")
    event_profile = repeat_to_net_discharge(base, remaining_ah)
    event_time_s = float(event_profile.time_s[-1])
    if not event_time_s < float(profile.time_s[-1]):
        raise ValueError("capacity event must occur strictly before profile end")
    return DriveWindowPlan(
        event_target_ah=remaining_ah,
        remaining_ah=remaining_ah,
        guard_ah=guard_ah,
        profile_available_ah=profile.net_discharge_ah,
        profile=profile,
        event_time_s=event_time_s,
        profile_fingerprint=profile.fingerprint,
    )


def validate_waveform(
    source: CurrentProfile | list[CurrentProfile],
    profile: CurrentProfile,
    *,
    period_s: int | None = None,
) -> WaveformValidation:
    sources = source if isinstance(source, list) else [source]
    selected_period = period_s or int(round(profile.time_s[-1]))
    units = [
        unit
        for segment in sources
        for unit in phase_average_segments_1hz([segment], selected_period)[1]
    ]
    return WaveformValidation(
        period_s=selected_period,
        segment_count=len(sources),
        complete_unit_count=len(units),
        source_rms_a=float(np.mean([item.rms_a for item in units])),
        profile_rms_a=profile.rms_a,
        source_min_a=float(np.mean([np.min(item.current_a) for item in units])),
        source_max_a=float(np.mean([np.max(item.current_a) for item in units])),
        profile_min_a=float(np.min(profile.current_a)),
        profile_max_a=float(np.max(profile.current_a)),
        source_recharge_ah=float(np.mean([item.recharge_ah for item in units])),
        profile_recharge_ah=profile.recharge_ah,
        source_net_discharge_ah=float(np.mean([item.net_discharge_ah for item in units])),
        profile_net_discharge_ah=profile.net_discharge_ah,
    )


def build_profile_from_mat(
    path: Path, candidate_min_s: int = 2400, candidate_max_s: int = 2800
) -> tuple[CurrentProfile, dict[str, object]]:
    segments = load_step14_segments(path)
    period, period_evidence = identify_repeat_period(segments, candidate_min_s, candidate_max_s)
    profile, units = phase_average_segments_1hz(segments, period)
    validation = validate_waveform(segments, profile, period_s=period).to_json()
    return profile, {
        "period_identification": period_evidence,
        "waveform_validation": validation,
        "complete_unit_count": len(units),
        "profile_fingerprint": profile.fingerprint,
    }


def validate_target(profile: CurrentProfile, target_ah: float, tolerance: float = 1e-3) -> None:
    error = abs(profile.net_discharge_ah - target_ah) / target_ah
    if error > tolerance:
        raise ValueError(f"UDDS target error {error:.3%} exceeds {tolerance:.3%}")
