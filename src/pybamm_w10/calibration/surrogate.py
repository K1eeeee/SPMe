"""Deterministic, solver-free stage-1 degradation candidate generation."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import math
from typing import Mapping

import numpy as np
from scipy.optimize import lsq_linear, minimize


MECHANISMS = ("sei", "plating", "lam")
MID_SCALE = 3.16
HIGH_SCALE = 10.0
PLATING_PROBE_SCALE = 1.5
PLATING_PROBE_ID = "PLATING-1P5"
RETIRED_PLATING_CANDIDATE_IDS = ("PLATING-M", "PLATING-H")
LOG10_BOUNDS = (-1.0, 1.0)
HIGH_PROBE_NOISE_FLOOR_PP = 0.05
HIGH_PROBE_GAP_FRACTION = 0.25
EQUIVALENT_RMSE_PP = 0.05
DIVERSE_RMSE_PP = 0.1


@dataclass(frozen=True)
class AgingCandidate:
    candidate_id: str
    scales: tuple[float, float, float]
    source: str
    parent_candidate: str | None = None
    stage: str = "PROBE"

    @property
    def log10_scales(self) -> tuple[float, float, float]:
        return tuple(math.log10(value) for value in self.scales)


@dataclass(frozen=True)
class ProbeResponse:
    candidate: AgingCandidate
    soh_25_pct: float | None
    soh_75_pct: float | None
    numerically_censored: bool = False
    retry_count: int = 0
    failure_class: str = "COMPLETED"


@dataclass(frozen=True)
class CombinationProposal:
    candidate_a: AgingCandidate
    candidate_b: AgingCandidate
    predicted_rmse_a_pp: float
    predicted_rmse_b_pp: float
    diversity_reason: str | None = None


def baseline_candidate() -> AgingCandidate:
    return AgingCandidate("BASELINE", (1.0, 1.0, 1.0), "fixed-baseline", stage="BASELINE")


def _scales(mechanism: str, value: float) -> tuple[float, float, float]:
    if mechanism not in MECHANISMS:
        raise ValueError(f"unknown degradation mechanism: {mechanism}")
    result = [1.0, 1.0, 1.0]
    result[MECHANISMS.index(mechanism)] = value
    return tuple(result)


def mid_rate_candidates() -> tuple[AgingCandidate, ...]:
    return (
        AgingCandidate("SEI-M", _scales("sei", MID_SCALE), "fixed-mid-probe"),
        AgingCandidate(
            PLATING_PROBE_ID,
            _scales("plating", PLATING_PROBE_SCALE),
            "fixed-plating-1p5-probe",
        ),
        AgingCandidate("LAM-M", _scales("lam", MID_SCALE), "fixed-mid-probe"),
    )


def requires_high_probe(baseline: ProbeResponse, mid: ProbeResponse, experimental_soh: Mapping[int, float]) -> bool:
    """Apply the cycle-75 trigger, treating a censored mid probe as unknown."""
    return bool(high_probe_reasons(baseline, mid, experimental_soh))


def high_probe_reasons(
    baseline: ProbeResponse, mid: ProbeResponse, experimental_soh: Mapping[int, float]
) -> tuple[str, ...]:
    """Return the fixed cycle-75 reasons that require a high probe."""
    if mid.numerically_censored:
        return (
            "mid_probe_physically_infeasible"
            if mid.failure_class == "PHYSICALLY_INFEASIBLE"
            else "mid_probe_numerically_censored",
        )
    if baseline.soh_25_pct is None or baseline.soh_75_pct is None or mid.soh_25_pct is None or mid.soh_75_pct is None:
        raise ValueError("high-probe decision requires complete baseline and mid SOH")
    if 25 not in experimental_soh or 75 not in experimental_soh:
        raise ValueError("high-probe decision requires experimental cycle 25 and 75 SOH")
    delta_25, delta_75 = mid.soh_25_pct - baseline.soh_25_pct, mid.soh_75_pct - baseline.soh_75_pct
    gap_75 = experimental_soh[75] - baseline.soh_75_pct
    improves_error = abs(mid.soh_75_pct - experimental_soh[75]) < abs(gap_75)
    reasons = []
    if not improves_error:
        reasons.append("does_not_reduce_cycle75_error")
    if abs(delta_75) < max(HIGH_PROBE_NOISE_FLOOR_PP, HIGH_PROBE_GAP_FRACTION * abs(gap_75)):
        reasons.append("cycle75_response_too_small")
    if delta_25 != 0 and delta_75 != 0 and math.copysign(1, delta_25) != math.copysign(1, delta_75):
        reasons.append("cycle25_cycle75_response_direction_changed")
    return tuple(reasons)


def high_rate_candidates(
    baseline: ProbeResponse, mids: Mapping[str, ProbeResponse], experimental_soh: Mapping[int, float]
) -> tuple[AgingCandidate, ...]:
    """Generate adaptive high probes except for the fixed 1.5x plating probe."""
    return tuple(
        AgingCandidate(f"{mechanism.upper()}-H", _scales(mechanism, HIGH_SCALE), "adaptive-high-probe", f"{mechanism.upper()}-M")
        for mechanism in MECHANISMS
        if mechanism != "plating"
        and requires_high_probe(baseline, mids[mechanism], experimental_soh)
    )


def choose_representative(
    responses: Mapping[str, tuple[ProbeResponse, ...]],
    baseline: ProbeResponse,
    experimental_soh_75: float,
) -> dict[str, ProbeResponse]:
    """Choose one valid response per mechanism using the approved stable priority."""
    if baseline.soh_75_pct is None:
        raise ValueError("representative selection requires baseline cycle 75 SOH")
    baseline_residual = baseline.soh_75_pct - experimental_soh_75
    result: dict[str, ProbeResponse] = {}
    for mechanism in MECHANISMS:
        choices = [item for item in responses[mechanism] if not item.numerically_censored and item.soh_75_pct is not None]
        if not choices:
            continue
        index = MECHANISMS.index(mechanism)

        def priority(item: ProbeResponse) -> tuple[object, ...]:
            residual = float(item.soh_75_pct) - experimental_soh_75
            improves_baseline = abs(residual) < abs(baseline_residual)
            crosses_experiment = baseline_residual * residual < 0.0
            log_amplitude = abs(item.candidate.log10_scales[index])
            return (
                not improves_baseline,
                crosses_experiment,
                abs(residual),
                log_amplitude,
                item.retry_count,
                item.candidate.candidate_id,
            )

        result[mechanism] = min(choices, key=priority)
    if set(result) != set(MECHANISMS):
        raise ValueError("a valid representative is required for each degradation mechanism")
    return result


def generate_combination_candidates(
    baseline: ProbeResponse,
    representatives: Mapping[str, ProbeResponse],
    experimental_soh: Mapping[int, float],
) -> CombinationProposal:
    """Fit local 25/75 response matrix, then make deterministic A/B proposals."""
    if baseline.soh_25_pct is None or baseline.soh_75_pct is None:
        raise ValueError("combination proposal requires baseline cycle 25/75 SOH")
    target = np.asarray([experimental_soh[25], experimental_soh[75]], dtype=float)
    baseline_values = np.asarray([baseline.soh_25_pct, baseline.soh_75_pct], dtype=float)
    matrix = np.empty((2, 3), dtype=float)
    for column, mechanism in enumerate(MECHANISMS):
        item = representatives[mechanism]
        if item.numerically_censored or item.soh_25_pct is None or item.soh_75_pct is None:
            raise ValueError("censored probes cannot enter the response matrix")
        amplitude = item.candidate.log10_scales[column]
        matrix[:, column] = (np.asarray([item.soh_25_pct, item.soh_75_pct]) - baseline_values) / amplitude
    response_target = target - baseline_values
    fit = lsq_linear(matrix, response_target, bounds=LOG10_BOUNDS, method="bvls")
    minimum = np.clip(fit.x, *LOG10_BOUNDS)
    minimum_rmse = float(np.sqrt(np.mean((matrix @ minimum - response_target) ** 2)))
    residual_limit = 2.0 * (minimum_rmse + EQUIVALENT_RMSE_PP) ** 2
    minimum_norm = minimize(
        lambda x: 0.5 * float(x @ x),
        minimum,
        jac=lambda x: x,
        bounds=[LOG10_BOUNDS] * len(MECHANISMS),
        constraints={
            "type": "ineq",
            "fun": lambda x: residual_limit - float((matrix @ x - response_target) @ (matrix @ x - response_target)),
            "jac": lambda x: -2.0 * matrix.T @ (matrix @ x - response_target),
        },
        method="SLSQP",
        options={"ftol": 1e-12, "maxiter": 200},
    )
    x_a = np.clip(minimum_norm.x if minimum_norm.success else minimum, *LOG10_BOUNDS)
    prediction_a = baseline_values + matrix @ x_a
    rmse_a = float(np.sqrt(np.mean((prediction_a - target) ** 2)))
    if not math.isfinite(rmse_a) or rmse_a > minimum_rmse + EQUIVALENT_RMSE_PP + 1e-8:
        x_a = minimum
        prediction_a = baseline_values + matrix @ x_a
        rmse_a = float(np.sqrt(np.mean((prediction_a - target) ** 2)))
    if rmse_a > minimum_rmse + EQUIVALENT_RMSE_PP + 1e-8:
        raise RuntimeError("candidate A is outside the approved equivalent-RMSE band")
    _, _, vectors = np.linalg.svd(matrix, full_matrices=True)
    direction = vectors[-1]
    candidates = []
    for sign in (1.0, -1.0):
        for step in (0.75, 0.5, 0.25, 0.1):
            x = np.clip(x_a + sign * step * direction, *LOG10_BOUNDS)
            predicted = baseline_values + matrix @ x
            rmse = float(np.sqrt(np.mean((predicted - target) ** 2)))
            if not np.allclose(x, x_a) and rmse <= rmse_a + DIVERSE_RMSE_PP:
                candidates.append((float(np.linalg.norm(x - x_a)), float(np.linalg.norm(x)), x, rmse))
    diversity_reason = None
    if candidates:
        _, _, x_b, rmse_b = min(candidates, key=lambda item: (-item[0], item[1], item[3], tuple(item[2])))
    else:
        grid = (
            np.asarray(values, dtype=float)
            for values in product((-1.0, -0.5, 0.0, 0.5, 1.0), repeat=len(MECHANISMS))
        )
        fallback = [
            (float(np.sqrt(np.mean((baseline_values + matrix @ x - target) ** 2))), float(np.linalg.norm(x)), x)
            for x in grid
            if not np.allclose(x, x_a)
        ]
        rmse_b, _, x_b = min(fallback, key=lambda item: (item[0], item[1], tuple(item[2])))
        diversity_reason = "fallback_predicted_second"
    to_candidate = lambda ident, values, source: AgingCandidate(
        ident, tuple(float(10.0 ** value) for value in values), source, stage="COMBINATION"
    )
    return CombinationProposal(to_candidate("A", x_a, "bounded-local-response"), to_candidate("B", x_b, "svd-diverse-local-response"), rmse_a, rmse_b, diversity_reason)
