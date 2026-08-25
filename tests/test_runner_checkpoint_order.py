from __future__ import annotations

import pickle

from pybamm_w10.config import RunConfig
from pybamm_w10.runner import W10Runner
from pybamm_w10.types import RPTResult, ProtocolPhase
from pybamm_w10.udds import CurrentProfile
import numpy as np


class FakeBackend:
    def snapshot(self):
        return "state"

    def current_time_s(self):
        return 123.0

    def compact_state(self):
        pass


def _rpt(node: int, capacity: float) -> RPTResult:
    return RPTResult(
        node=node,
        q_rpt_start_ah=10.0,
        q_rpt_end_ah=10.0 + capacity,
        capacity_ah=capacity,
        soh_initial_pct=90.0,
        soh_nominal_pct=80.0,
        mode="virtual",
        start_time_s=0.0,
        end_time_s=1.0,
        diagnostic_duration_s=1.0,
        changed_main_state=False,
        main_state_hash_before="same",
        main_state_hash_after="same",
        main_time_before_s=0.0,
        main_time_after_s=0.0,
        main_capacity_before_ah=0.0,
        main_capacity_after_ah=0.0,
        became_q_ref=node < 350,
        timeseries={"time_s": np.array([0.0, 1.0]), "current_a": np.array([0.0, 0.0])},
    )


def test_rpt_checkpoint_contains_new_qref_before_atomic_commit(workspace_tmp, monkeypatch) -> None:
    import pybamm_w10.runner as runner_module

    monkeypatch.setattr(runner_module, "run_capacity_rpt", lambda *_args, **_kwargs: _rpt(25, 3.6))
    config = RunConfig()
    runner = W10Runner(config, workspace_tmp)
    run_dir = workspace_tmp / "run"
    run_dir.mkdir()
    for child in ("checkpoints", "timeseries", "figures", "rollback"):
        (run_dir / child).mkdir()
    profile = CurrentProfile(np.array([0.0, 1.0]), np.array([1.0, 1.0]))
    values = runner._run_rpt_transaction(
        FakeBackend(), 25, 4.0, 0, 4.0, profile, 7, 0, run_dir,
        "input", "initial", "environment",
    )
    assert values[0:2] == (3.6, 25)
    with (run_dir / "checkpoints" / "cycle-025.pkl").open("rb") as handle:
        checkpoint = pickle.load(handle)
    assert checkpoint.q_ref_ah == 3.6
    assert checkpoint.q_ref_node == 25
    assert checkpoint.capacity_targets.q_ref_ah == 3.6
    assert checkpoint.result_transaction == 8


def test_cycle350_rpt_does_not_create_unused_targets(workspace_tmp, monkeypatch) -> None:
    import pybamm_w10.runner as runner_module

    monkeypatch.setattr(runner_module, "run_capacity_rpt", lambda *_args, **_kwargs: _rpt(350, 3.0))
    runner = W10Runner(RunConfig(), workspace_tmp)
    run_dir = workspace_tmp / "run350"
    run_dir.mkdir()
    for child in ("checkpoints", "timeseries", "figures", "rollback"):
        (run_dir / child).mkdir()
    profile = CurrentProfile(np.array([0.0, 1.0]), np.array([1.0, 1.0]))
    q_ref, node, targets, _, _, phase = runner._run_rpt_transaction(
        FakeBackend(), 350, 3.5, 325, 4.0, profile, 9, 325, run_dir,
        "input", "initial", "environment",
    )
    assert (q_ref, node) == (3.5, 325)
    assert targets is None
    assert phase == ProtocolPhase.CYCLE_COMPLETED
