from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from pybamm_w10.calibration.capacity import (
    CAPACITY_SEARCH_MAX_EVALUATIONS,
    CapacityCalibrationError,
    CapacityCandidate,
    _extract_rpt_discharge_curve,
    run_capacity_search,
)
from pybamm_w10.config import RunConfig


def _candidate(scale: float) -> CapacityCandidate:
    # An exactly monotonic synthetic SPMe response; no PyBaMM solve is involved.
    capacity = 4.6 + 3.0 * (scale - 0.90)
    curve_capacity = np.array([0.0, capacity / 2, capacity])
    return CapacityCandidate(
        scale_factor=scale,
        capacity_ah=capacity,
        discharge_capacity_ah=curve_capacity,
        voltage_v=np.array([4.2, 3.5, 2.5]),
        initial_state_fingerprint=f"initial-{scale:.8f}",
        parameter_fingerprint=f"parameters-{scale:.8f}",
    )


def test_bounded_search_uses_fresh_candidates_and_repeat_within_budget() -> None:
    calls: list[float] = []

    def solve(scale: float) -> CapacityCandidate:
        calls.append(scale)
        return _candidate(scale)

    result = run_capacity_search(solve)

    assert result.converged
    assert result.winner.relative_error <= 0.002
    assert result.bracket_width <= 1e-4
    assert result.repeat_relative_difference <= 0.0002
    assert len(calls) == len(result.candidates) + 1  # final call is the independent repeat
    assert len(calls) <= CAPACITY_SEARCH_MAX_EVALUATIONS
    assert len({candidate.initial_state_fingerprint for candidate in result.candidates}) == len(result.candidates)


def test_search_rejects_unbracketed_or_nonmonotonic_capacity_response() -> None:
    with pytest.raises(CapacityCalibrationError, match="bracket"):
        run_capacity_search(lambda scale: replace(_candidate(scale), capacity_ah=4.0 + scale / 10))

    with pytest.raises(CapacityCalibrationError, match="monotonic"):
        run_capacity_search(lambda scale: replace(_candidate(scale), capacity_ah=6.0 - scale))


def test_candidate_solution_is_strict_cycle_zero_and_has_no_aging_surface(monkeypatch) -> None:
    # The public search API receives a one-argument candidate evaluator.  It
    # has no backend/checkpoint/cycle-number input through which aging state
    # could be resumed or scheduled.
    assert list(run_capacity_search.__annotations__)[:1] == ["solve_candidate"]
    assert "run_standard_cycle" not in Path(__file__).parents[2].joinpath(
        "src", "pybamm_w10", "calibration", "capacity.py"
    ).read_text(encoding="utf-8")


def test_rpt_curve_extraction_excludes_charge_and_rest_segments() -> None:
    capacity, voltage = _extract_rpt_discharge_curve(
        {
            "current_a": np.array([-4.85, -0.05, 0.0, 0.24, 0.24]),
            "discharge_capacity_ah": np.array([1.0, 0.5, 0.5, 0.6, 1.0]),
            "terminal_voltage_v": np.array([4.0, 4.2, 4.2, 3.7, 2.5]),
        },
        0.5,
        1.0,
    )
    assert capacity.tolist() == pytest.approx([0.1, 0.5])
    assert voltage.tolist() == pytest.approx([3.7, 2.5])


def test_calibration_writes_only_cycle_zero_artifacts(monkeypatch, workspace_tmp) -> None:
    import pybamm_w10.calibration.capacity as capacity_module

    output_dir = workspace_tmp / "outputs" / "m50t-w10-v1"
    config = RunConfig(mode="strict-w10", data_root=workspace_tmp / "read-only-data")
    monkeypatch.setattr(capacity_module, "build_spme", lambda *_: object())
    monkeypatch.setattr(
        capacity_module,
        "effective_parameters_audit",
        lambda *_args, **_kwargs: {"fingerprint": "a" * 64, "rpt": {"cycle_0_capacity_ah": 4.866}},
    )
    inventory = {
        "inventory_schema_version": 1,
        "aging_calibration_gate": {"status": "AGING_DATA_INCOMPLETE", "reason": "MISSING_W10_HPPC_EIS"},
    }
    result = capacity_module.run_capacity_calibration(
        config,
        output_dir,
        candidate_solver=_candidate,
        inventory_builder=lambda _: inventory,
        experimental_curve_loader=lambda _: (
            np.array([0.0, 4.865884391243259 / 2, 4.865884391243259]),
            np.array([4.2, 3.5, 2.5]),
        ),
    )

    assert result.converged
    for filename in (
        "calibration_config.json",
        "diagnostic_inventory.json",
        "capacity_search.csv",
        "capacity_calibration.json",
        "voltage_curve_comparison.csv",
        "effective_parameters.json",
        "calibrated_parameters.json",
        "calibration_status.json",
        "run.log",
    ):
        assert (output_dir / filename).is_file()
    assert (output_dir / "figures" / "cycle0_voltage_comparison.png").is_file()
    assert (output_dir / "candidates" / "candidate-001" / "run.log").is_file()
    assert not list(output_dir.rglob("cycle_summary.csv"))
    assert not list(output_dir.rglob("degradation_summary.csv"))
