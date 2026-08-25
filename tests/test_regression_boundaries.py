"""Regression boundaries for the isolated W10 remediation workspace.

These checks intentionally inspect configuration only. They must never invoke
``W10Runner.run()`` or a PyBaMM solve, because their job is to lock the
pre-remediation baseline used by the following implementation stages.
"""

from __future__ import annotations

import pytest

from pybamm_w10.config import RunConfig


def test_baseline_w10_configuration_is_explicit_and_unchanged() -> None:
    """Keep the accepted pre-remediation protocol constants visible in tests."""
    config = RunConfig()

    assert config.initial_soc == pytest.approx(0.20)
    assert config.cell.nominal_capacity_ah == pytest.approx(4.85)
    assert config.protocol.charge_3c_a == pytest.approx(14.55)
    assert config.protocol.charge_3c_a / config.cell.nominal_capacity_ah == pytest.approx(3.0)
    assert config.cell.lower_cutoff_v == pytest.approx(2.5)
    assert config.cell.upper_cutoff_v == pytest.approx(4.2)
    assert config.protocol.rest_after_charge_s == 30 * 60
    assert config.udds_period_min_s <= 2600 <= config.udds_period_max_s
