from __future__ import annotations

from pybamm_w10.charge_efficiency import assess_charge_efficiency_status
from pybamm_w10.types import ChargeEfficiencyStatus


def test_status_priority_and_stable_flags() -> None:
    status = assess_charge_efficiency_status(
        invalid_external_charge=True,
        balance_abs_error_pct=2.0,
        plating_crosscheck_failed=True,
        reversible_plating_increment_ah=-0.1,
    )

    assert status.primary_status is ChargeEfficiencyStatus.INVALID_EXTERNAL_CHARGE
    assert status.status_flags == (
        ChargeEfficiencyStatus.INVALID_EXTERNAL_CHARGE,
        ChargeEfficiencyStatus.CHARGE_BALANCE_FAILURE,
        ChargeEfficiencyStatus.PLATING_INVENTORY_CROSSCHECK_FAILURE,
        ChargeEfficiencyStatus.PREEXISTING_PLATED_LITHIUM_RELEASED,
    )
    assert status.is_valid_for_efficiency_analysis is False
    assert status.is_valid_for_mechanism_analysis is False


def test_balance_warning_is_valid_but_explicit() -> None:
    status = assess_charge_efficiency_status(balance_abs_error_pct=0.5)
    assert status.primary_status is ChargeEfficiencyStatus.BALANCE_WARNING
    assert status.is_valid_for_efficiency_analysis is True
