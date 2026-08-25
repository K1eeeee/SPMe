from __future__ import annotations

import json

from pybamm_w10.calibration.parameters import CalibrationParameters
from pybamm_w10.cli import main


def test_cli_rejects_workspace_outside_isolated_project(capsys, workspace_tmp) -> None:
    code = main(["--workspace", str(workspace_tmp), "--data-root", r"E:\battery\data", "--prepare"])
    assert code == 2
    assert "workspace" in capsys.readouterr().out.lower()


def test_cli_rejects_unfrozen_parameters_for_formal_run(workspace_tmp, capsys) -> None:
    path = workspace_tmp / "params.json"
    path.write_text(json.dumps(CalibrationParameters().to_json()), encoding="utf-8")
    code = main(
        [
            "--workspace", r"E:\SPMe",
            "--data-root", r"E:\battery\data",
            "--run",
            "--calibration-params", str(path),
        ]
    )
    assert code == 2
    assert "PARAMETERS_FROZEN" in capsys.readouterr().out


def test_cli_calibrate_capacity_defaults_to_strict_w10_and_is_not_an_aging_action(monkeypatch, capsys) -> None:
    import pybamm_w10.cli as cli

    observed: dict[str, object] = {}

    class Result:
        class Winner:
            scale_factor = 1.0
            capacity_ah = 4.865884391243259
            relative_error = 0.0

        winner = Winner()
        repeat_relative_difference = 0.0

    def fake_calibration(config, output_dir):
        observed["mode"] = config.mode
        observed["output_dir"] = output_dir
        return Result()

    monkeypatch.setattr(cli, "ensure_required_interpreter", lambda config: None)
    monkeypatch.setattr(cli, "run_capacity_calibration", fake_calibration)
    code = main(
        [
            "--workspace", r"E:\SPMe",
            "--data-root", r"E:\battery\data",
            "--calibrate-capacity",
        ]
    )
    assert code == 0
    assert observed["mode"] == "strict-w10"
    assert str(observed["output_dir"]).endswith(r"outputs\pybamm_spme_calibration\m50t-w10-v1")
    assert "CAPACITY_CALIBRATED" in capsys.readouterr().out


def test_cli_starts_only_explicit_baseline_repaired_aging_verification(monkeypatch, capsys) -> None:
    import pybamm_w10.cli as cli
    from pybamm_w10.types import RunStatus

    observed: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, config, workspace, calibration_parameters):
            observed["config"] = config
            observed["calibration_parameters"] = calibration_parameters

        def run(self, output_dir):
            observed["output_dir"] = output_dir
            return RunStatus.COMPLETED

    capacity_parameters = CalibrationParameters(
        calibration_status="CAPACITY_CALIBRATED",
        capacity_scale_factor=0.95630859375,
    )
    monkeypatch.setattr(cli, "W10Runner", FakeRunner)
    monkeypatch.setattr(cli, "load_calibration_parameters", lambda path: capacity_parameters)
    monkeypatch.setattr(cli, "write_json", lambda path, value: observed.update(authorization=value))
    code = main(
        [
            "--workspace", r"E:\SPMe",
            "--data-root", r"E:\battery\data",
            "--verify-repaired-aging",
            "--calibration-params", r"E:\SPMe\inputs\spme_transferred_parameters.json",
            "--output-dir", r"E:\SPMe\outputs\pybamm_spme\pytest-verify-repaired-aging-contract",
        ]
    )

    assert code == 0
    assert observed["config"].mode == "virtual"
    assert observed["calibration_parameters"] == capacity_parameters
    assert observed["authorization"]["capacity_scale_factor"] == 0.95630859375
    assert observed["authorization"]["model"] == "SPMe"
    assert observed["authorization"]["formal_calibrated_prediction"] is False
    assert str(observed["output_dir"]).endswith(r"outputs\pybamm_spme\pytest-verify-repaired-aging-contract")


def test_cli_repaired_aging_verification_rejects_missing_capacity_parameters(capsys) -> None:
    code = main(
        [
            "--workspace", r"E:\SPMe",
            "--data-root", r"E:\battery\data",
            "--verify-repaired-aging",
            "--output-dir", r"E:\SPMe\outputs\pybamm_spme\pytest-verify-repaired-aging-missing-params",
        ]
    )

    assert code == 2
    assert "requires --calibration-params" in capsys.readouterr().out


def test_cli_evaluates_completed_run_without_starting_model(monkeypatch, capsys) -> None:
    import pybamm_w10.evaluation as evaluation

    observed: dict[str, object] = {}

    def fake_evaluation(run_dir, data_root):
        observed["run_dir"] = run_dir
        observed["data_root"] = data_root
        return evaluation.SohAccuracyMetrics(
            node_count=15,
            soh_mae_pp=1.0,
            soh_rmse_pp=1.2,
            soh_max_abs_error_pp=2.0,
            soh_final_error_pp=-0.5,
            capacity_rmse_ah=0.05,
            soh_r_squared=0.98,
        )

    monkeypatch.setattr(evaluation, "evaluate_soh_comparison", fake_evaluation)
    code = main(
        [
            "--workspace", r"E:\SPMe",
            "--data-root", r"E:\battery\data",
            "--evaluate-soh", r"E:\SPMe\outputs\pybamm_spme\completed-run",
        ]
    )

    assert code == 0
    assert str(observed["run_dir"]).endswith(r"outputs\pybamm_spme\completed-run")
    assert str(observed["data_root"]) == r"E:\battery\data"
    assert "SOH_EVALUATED" in capsys.readouterr().out
