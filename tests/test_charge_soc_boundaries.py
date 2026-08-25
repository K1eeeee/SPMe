from __future__ import annotations

import pytest

from pybamm_w10.charge_efficiency import (
    find_soc_boundaries,
    merge_charge_stage_traces,
    reference_soc_is_nonmonotonic,
)


def test_stage_join_deduplicates_boundary_in_favour_of_the_later_stage() -> None:
    trace = merge_charge_stage_traces(
        (
            ("3c_cc", (0.0, 1.0), (-1.0, -1.0)),
            ("4v_cv", (1.0, 2.0), (-0.5, -0.2)),
        )
    )

    assert trace.time_s == (0.0, 1.0, 2.0)
    assert trace.stage_names == ("3c_cc", "4v_cv", "4v_cv")
    assert trace.current_a == pytest.approx((-1.0, -0.5, -0.2))


def test_soc_boundaries_choose_first_upward_crossing_and_report_all_crossings() -> None:
    result = find_soc_boundaries(
        (0.0, 1.0, 2.0, 3.0, 4.0),
        (20.0, 45.0, 35.0, 45.0, 100.0),
        (40.0, 100.0),
    )

    assert result[40.0].time_s == pytest.approx(0.8)
    assert result[40.0].crossing_count == 2
    assert result[100.0].time_s == pytest.approx(4.0)


def test_unreached_boundary_and_nonmonotonic_path_are_explicit() -> None:
    result = find_soc_boundaries((0.0, 1.0), (20.0, 60.0), (80.0,))
    assert result[80.0].time_s is None
    assert result[80.0].crossing_count == 0
    assert reference_soc_is_nonmonotonic((20.0, 20.000002, 20.0), tolerance_pct=1e-6)
