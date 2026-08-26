from __future__ import annotations

from dataclasses import replace

from pybamm_w10.config import RunConfig
from pybamm_w10.model import (
    build_solver,
    certified_charge_solver_profile,
    conservative_charge_solver_profile,
    conservative_solver_profile,
    default_solver_profile,
)


def test_solver_profiles_preserve_tolerances_and_only_harden_integration() -> None:
    config = RunConfig()
    default = default_solver_profile(config)
    conservative = conservative_solver_profile(config)

    assert (default.rtol, default.atol) == (1e-5, 1e-7)
    assert (conservative.rtol, conservative.atol) == (1e-5, 1e-7)
    assert default.dt_init_s == 0.0
    assert conservative.dt_init_s == 1e-8
    assert default.max_error_test_failures == 10
    assert conservative.max_error_test_failures == 30
    assert default.max_order_bdf == 5
    assert conservative.max_order_bdf == 3
    assert default.suppress_algebraic_error is False
    assert conservative.suppress_algebraic_error is True
    assert default.max_step_s == conservative.max_step_s == 1.0

    solver = build_solver(config, conservative)
    assert solver.options["dt_init"] == 1e-8
    assert solver.options["dt_max"] == 1.0
    assert solver.options["max_error_test_failures"] == 30
    assert solver.options["max_order_bdf"] == 3
    assert solver.options["suppress_algebraic_error"] is True


def test_charge_profiles_are_fixed_and_retry_preserves_charge_settings() -> None:
    config = RunConfig()
    charge = certified_charge_solver_profile(config)
    retry = conservative_charge_solver_profile(config)

    assert charge.name == "certified_charge"
    assert (
        charge.rtol,
        charge.atol,
        charge.dt_init_s,
        charge.max_step_s,
        charge.max_order_bdf,
        charge.suppress_algebraic_error,
        charge.max_error_test_failures,
        charge.max_num_steps,
    ) == (1e-5, 1e-7, 1e-8, 1.0, 3, True, 30, 200_000)
    assert retry.name == "certified_charge_retry"
    assert retry == replace(charge, name="certified_charge_retry")
