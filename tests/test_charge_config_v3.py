from __future__ import annotations

from dataclasses import replace

import pytest

from pybamm_w10.config import RunConfig


def test_charge_efficiency_v3_constants_are_fingerprinted() -> None:
    config = RunConfig()

    assert config.output_schema_version == 3
    assert config.checkpoint_schema_version == 6
    assert config.protocol_algorithm_version == "w10-window-v3-charge-efficiency"
    assert config.solver_execution_version == "stage-local-time-v2-robust-charge"
    assert config.solver_profile_policy_version == "phase-fixed-v1"
    assert config.solver_attempt_audit_version == "solver-attempt-v1"
    assert (config.solver.rtol, config.solver.atol, config.solver.max_step_s) == (1e-5, 1e-7, 1.0)
    assert (
        config.solver.charge_rtol,
        config.solver.charge_atol,
        config.solver.charge_max_step_s,
    ) == (1e-5, 1e-7, 1.0)
    assert config.soc_boundaries_pct == (20.0, 40.0, 60.0, 80.0, 100.0)
    assert config.charge_efficiency_algorithm_version == "charge-efficiency-v1"
    assert replace(config, charge_balance_pass_limit_pct=0.1).fingerprint() != config.fingerprint()
    assert replace(config, solver_execution_version="other").fingerprint() != config.fingerprint()
    assert replace(config, solver_profile_policy_version="other").fingerprint() != config.fingerprint()
    assert replace(config, solver=replace(config.solver, charge_max_step_s=0.2)).guard_fingerprint() != config.guard_fingerprint()


@pytest.mark.parametrize(
    "kwargs",
    (
        {"soc_boundaries_pct": (20.0, 40.0, 40.0, 80.0, 100.0)},
        {"charge_balance_pass_limit_pct": 1.0, "charge_balance_failure_limit_pct": 1.0},
        {"soc_boundary_residual_tolerance_pct": 0.0},
    ),
)
def test_charge_efficiency_config_rejects_invalid_thresholds(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RunConfig(**kwargs)
