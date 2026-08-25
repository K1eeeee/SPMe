from dataclasses import replace

from pybamm_w10.config import RunConfig
from pybamm_w10.runner import should_evaluate_full_soh


def test_full_soh_evaluation_is_skipped_for_bounded_regression() -> None:
    base = RunConfig()
    bounded = replace(
        base,
        protocol=replace(base.protocol, max_aging_cycles=25, rpt_nodes=(0, 25)),
    )

    assert not should_evaluate_full_soh(bounded)
    assert should_evaluate_full_soh(base)
