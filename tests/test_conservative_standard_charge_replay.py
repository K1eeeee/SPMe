from __future__ import annotations

import importlib.util
from pathlib import Path

from pybamm_w10.config import RunConfig


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "replay_cycle1_122_conservative_standard_charges.py"
    spec = importlib.util.spec_from_file_location("conservative_charge_replay", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_profile_exactly_matches_requested_values() -> None:
    module = _module()
    profile = module.fixed_conservative_profile(RunConfig())

    assert profile.name == "fixed_conservative_standard_charge"
    assert profile.rtol == 1e-5
    assert profile.atol == 1e-7
    assert profile.dt_init_s == 1e-8
    assert profile.max_step_s == 1.0
    assert profile.max_order_bdf == 3
    assert profile.suppress_algebraic_error is True
    assert profile.max_error_test_failures == 30
    assert profile.max_num_steps == 200_000


def test_charge_cycle_maps_to_preceding_read_only_checkpoint(workspace_tmp: Path) -> None:
    module = _module()
    source = workspace_tmp / "source"

    assert module.checkpoint_for_charge(source, 1) == source / "checkpoints" / "cycle-000.pkl"
    assert module.checkpoint_for_charge(source, 122) == source / "checkpoints" / "cycle-121.pkl"
