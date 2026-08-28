from __future__ import annotations

from pybamm_w10.calibration.surrogate import (
    baseline_candidate,
    choose_representative,
    generate_combination_candidates,
    high_rate_candidates,
    mid_rate_candidates,
    ProbeResponse,
)


def test_stage1_probe_budget_is_fixed_and_minimal() -> None:
    assert baseline_candidate().scales == (1.0, 1.0, 1.0)
    candidates = mid_rate_candidates()
    assert [item.candidate_id for item in candidates] == ["SEI-M", "PLATING-1P5", "LAM-M"]
    assert [item.scales for item in candidates] == [(3.16, 1.0, 1.0), (1.0, 1.5, 1.0), (1.0, 1.0, 3.16)]


def test_high_probe_generation_never_escalates_the_fixed_plating_probe() -> None:
    baseline = ProbeResponse(baseline_candidate(), 99.0, 98.0)
    mids = {
        "sei": ProbeResponse(mid_rate_candidates()[0], 98.9, 97.5),
        "plating": ProbeResponse(mid_rate_candidates()[1], 99.0, 98.0, numerically_censored=True),
        "lam": ProbeResponse(mid_rate_candidates()[2], 99.1, 97.5),
    }
    generated = high_rate_candidates(baseline, mids, {25: 99.0, 75: 97.0})
    assert [item.candidate_id for item in generated] == ["LAM-H"]
    assert all(item.candidate_id not in {"PLATING-M", "PLATING-H"} for item in generated)


def test_combination_a_uses_minimum_norm_equivalent_solution_and_b_is_distinct() -> None:
    baseline = ProbeResponse(baseline_candidate(), 100.0, 100.0)
    representatives = {
        mechanism: ProbeResponse(candidate, 99.0, 99.0)
        for mechanism, candidate in zip(("sei", "plating", "lam"), mid_rate_candidates(), strict=True)
    }

    proposal = generate_combination_candidates(baseline, representatives, {25: 98.0, 75: 98.0})

    assert proposal.predicted_rmse_a_pp <= 0.05 + 1e-8
    assert all(0.1 <= value <= 10.0 for value in proposal.candidate_a.scales)
    assert proposal.candidate_a.log10_scales != proposal.candidate_b.log10_scales


def test_representative_prefers_improvement_without_crossing_experiment() -> None:
    baseline = ProbeResponse(baseline_candidate(), 99.0, 99.0)
    mids = mid_rate_candidates()
    from pybamm_w10.calibration.surrogate import AgingCandidate

    responses = {
        "sei": (
            ProbeResponse(mids[0], 98.0, 97.9),
            ProbeResponse(AgingCandidate("SEI-H", (10.0, 1.0, 1.0), "test"), 97.0, 96.9),
        ),
        "plating": (ProbeResponse(mids[1], 98.0, 98.4),),
        "lam": (ProbeResponse(mids[2], 98.0, 98.3),),
    }

    selected = choose_representative(responses, baseline, 98.0)

    assert selected["sei"].candidate.candidate_id == "SEI-M"
