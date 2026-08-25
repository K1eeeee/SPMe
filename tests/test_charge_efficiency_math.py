from __future__ import annotations

import math

import pytest

from pybamm_w10.charge_efficiency import (
    charge_balance,
    integrate_charge_stages,
    integrate_stage_charge_ah,
    reference_soc_pct,
)


def test_stage_local_trapezoid_does_not_integrate_across_stage_current_jump() -> None:
    stages = (
        ((0.0, 3600.0), (-1.0, -1.0)),
        ((3600.0, 7200.0), (-3.0, -3.0)),
    )

    assert integrate_stage_charge_ah(*stages[0]) == pytest.approx(1.0, abs=1e-12)
    result = integrate_charge_stages(stages, cc_stage_indexes=(0, 1), cv_stage_indexes=())
    assert result.external_charge_ah == pytest.approx(4.0, abs=1e-12)
    assert result.cc_charge_ah == pytest.approx(4.0, abs=1e-12)
    assert result.cv_charge_ah == pytest.approx(0.0, abs=1e-12)


def test_charge_integral_ignores_discharge_current() -> None:
    assert integrate_stage_charge_ah((0.0, 1800.0, 3600.0), (1.0, 1.0, -2.0)) == pytest.approx(0.5)


def test_reference_soc_uses_faraday_particle_lithium_delta() -> None:
    faraday = 96485.33212
    delta_mol = 2.0 * 3600.0 / faraday
    values = reference_soc_pct((10.0, 10.0 + delta_mol), 2.0, faraday_constant_c_per_mol=faraday)

    assert values == pytest.approx((20.0, 120.0))


def test_charge_balance_and_efficiencies_preserve_values_above_100_percent() -> None:
    result = charge_balance(
        external_charge_ah=1.0,
        intercalated_charge_increment_ah=1.1,
        reversible_plating_increment_ah=-0.05,
        dead_lithium_increment_ah=0.01,
        sei_increment_ah=0.01,
    )

    assert result.useful_charge_efficiency_pct == pytest.approx(110.0)
    assert result.reversible_retention_pct == pytest.approx(105.0)
    assert result.reversible_plating_depletion_ah == pytest.approx(0.05)
    assert result.accounted_charge_ah == pytest.approx(1.07)
    assert result.charge_balance_error_pct == pytest.approx(-7.0)


@pytest.mark.parametrize("external", (0.0, -1.0, math.nan))
def test_charge_balance_marks_invalid_denominator(external: float) -> None:
    result = charge_balance(external, 0.5, 0.0, 0.0, 0.0)
    assert result.useful_charge_efficiency_pct is None
    assert result.reversible_retention_pct is None
