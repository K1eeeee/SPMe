"""Contracts for the user-facing bounded cycle-122 validation scripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _script_module(filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cycle122_entry_has_fixed_nonformal_schedule(workspace_tmp: Path) -> None:
    module = _script_module("run_cycle0_122_capacity_validation.py")

    config = module.cycle122_validation_config(
        workspace_tmp,
        workspace_tmp / "data",
        workspace_tmp / "inputs" / "parameters.json",
    )

    assert config.mode == "virtual"
    assert config.protocol.max_aging_cycles == 122
    assert config.protocol.rpt_nodes == (0, 25, 75, 122)


def test_cycle122_resume_is_restricted_to_matching_run_checkpoint(
    workspace_tmp: Path,
) -> None:
    module = _script_module("run_cycle0_122_capacity_validation.py")
    config = module.cycle122_validation_config(
        workspace_tmp,
        workspace_tmp / "data",
        workspace_tmp / "inputs" / "parameters.json",
    )
    run_dir = workspace_tmp / "outputs" / "cycle122"
    checkpoint = run_dir / "checkpoints" / "cycle-009.pkl"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint-placeholder")
    (run_dir / "cycle122_validation_scope.json").write_text(
        json.dumps({
            "kind": "cycle_0_122_capacity_validation",
            "starts_from_cycle": 0,
            "max_aging_cycles": 122,
            "rpt_nodes": [0, 25, 75, 122],
            "config_fingerprint": config.fingerprint(),
        }),
        encoding="utf-8",
    )

    assert module.validate_cycle122_resume_target(run_dir, checkpoint, config) == checkpoint
    outside = workspace_tmp / "outside" / "cycle-009.pkl"
    outside.parent.mkdir()
    outside.write_bytes(b"checkpoint-placeholder")
    with pytest.raises(ValueError, match="same validation run"):
        module.validate_cycle122_resume_target(run_dir, outside, config)


def test_cycle122_comparator_writes_normalized_node122_error(
    workspace_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _script_module("compare_w10_cycle122_capacity.py")
    run_dir = workspace_tmp / "run"
    data_root = workspace_tmp / "data"
    run_dir.mkdir()
    (run_dir / "run_status.json").write_text('{"status":"COMPLETED"}', encoding="utf-8")
    (run_dir / "rpt_summary.csv").write_text(
        "node,capacity_ah\n0,5.0\n25,4.9\n75,4.8\n122,4.7\n", encoding="utf-8"
    )
    diagnostics = data_root / "LG M50T" / "_processed_mat"
    diagnostics.mkdir(parents=True)
    header = "diagnostic_number,cell_id,time_s,current_a,voltage_v,capacity_ah\n"
    (diagnostics / "W10_capacity_diagnostic_01.csv").write_text(
        header + "1,W10,0,0,4.2,5.0\n", encoding="utf-8"
    )
    (diagnostics / "W10_capacity_diagnostic_04.csv").write_text(
        header + "4,W10,0,0,4.2,4.6\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_w10_cycle122_capacity.py",
            "--run-dir", str(run_dir),
            "--data-root", str(data_root),
        ],
    )

    assert module.main() == 0
    payload = json.loads((run_dir / "cycle122_capacity_accuracy.json").read_text(encoding="utf-8"))
    assert payload["cycle"] == 122
    assert payload["capacity_error_ah"] == pytest.approx(0.1)
    assert payload["simulated_soh_pct"] == pytest.approx(94.0)
    assert payload["experimental_soh_pct"] == pytest.approx(92.0)
    assert payload["absolute_soh_error_percentage_points"] == pytest.approx(2.0)
