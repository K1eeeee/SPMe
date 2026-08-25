from __future__ import annotations

from pybamm_w10.backend import build_standard_charge_experiment
from pybamm_w10.config import RPT_NODES, RunConfig


def test_standard_charge_experiment_preserves_protocol_constants() -> None:
    config = RunConfig()
    experiment = build_standard_charge_experiment(config)

    assert experiment.cycle_lengths == [4]
    assert tuple(step.value for step in experiment.steps) == (-14.55, 4.0, -1.2125, 4.2)
    assert tuple(float(step.termination[0].value) for step in experiment.steps) == (
        4.0, 0.05, 4.2, 0.05,
    )
    assert config.protocol.rest_after_charge_s == 1800
    assert config.protocol.rpt_nodes == RPT_NODES
    assert config.solver.rtol == 1e-5
    assert config.solver.atol == 1e-7


def test_solver_resilience_does_not_change_physical_protocol_versions() -> None:
    config = RunConfig()

    assert config.output_schema_version == 3
    assert config.protocol_algorithm_version == "w10-window-v3-charge-efficiency"
