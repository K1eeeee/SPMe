from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from pybamm_w10.calibration.aging import AgingCalibrationError, Stage1AgingCalibration, _latest_checkpoint
from pybamm_w10.calibration.data import W10_DIAGNOSTIC_NODES
from pybamm_w10.calibration.parameters import CalibrationParameters
from pybamm_w10.calibration.surrogate import baseline_candidate
from pybamm_w10.config import RunConfig
from pybamm_w10.progress import ProgressState
from pybamm_w10.types import RunStatus

from .test_data_inventory import make_w10_data_root


class _FakeRunner:
    calls: list[tuple[str, int]] = []

    def __init__(self, config, workspace, parameters) -> None:
        self.config = config

    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        self.calls.append((output_dir.name, stop_after_cycle))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "checkpoints").mkdir(exist_ok=True)
        (output_dir / "checkpoints" / f"cycle-{stop_after_cycle:03d}.pkl").write_bytes(b"fake")
        cycle_path = output_dir / "cycle_summary.csv"
        completed = set()
        if cycle_path.is_file():
            with cycle_path.open(newline="", encoding="utf-8") as handle:
                completed = {int(row["cycle"]) for row in csv.DictReader(handle)}
        completed.add(stop_after_cycle)
        with cycle_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("cycle",))
            writer.writeheader()
            writer.writerows({"cycle": cycle} for cycle in sorted(completed))
        path = output_dir / "rpt_summary.csv"
        existing = {}
        if path.is_file():
            with path.open(newline="", encoding="utf-8") as handle:
                existing = {int(row["node"]): float(row["capacity_ah"]) for row in csv.DictReader(handle)}
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("node", "capacity_ah"))
            writer.writeheader()
            for index, node in enumerate(W10_DIAGNOSTIC_NODES, start=1):
                if node <= stop_after_cycle:
                    existing[node] = 4.9 - index / 100
            for node, capacity in sorted(existing.items()):
                writer.writerow({"node": node, "capacity_ah": capacity})
        progress_callback(ProgressState(phase="CYCLE_COMPLETED", completed_cycles=stop_after_cycle, current_cycle=stop_after_cycle))
        return RunStatus.PAUSED


def test_stage1_workflow_uses_fake_runner_and_keeps_holdout_gated(workspace_tmp) -> None:
    _FakeRunner.calls = []
    data_root = make_w10_data_root(workspace_tmp / "data")
    output_dir = workspace_tmp / "stage1"
    result = Stage1AgingCalibration(
        RunConfig(data_root=data_root),
        Path(r"E:\SPMe"),
        output_dir,
        CalibrationParameters(calibration_status="CAPACITY_CALIBRATED", capacity_scale_factor=0.95630859375),
        runner_factory=_FakeRunner,
        progress_writer=lambda message: None,
    ).run()

    assert result["status"] == "COMPLETED"
    assert (output_dir / "frozen_parameters.json").is_file()
    assert (output_dir / "holdout_access.json").is_file()
    assert (output_dir / "stage1_soh_comparison.csv").is_file()
    assert (output_dir / "figures" / "stage1_soh_sim_vs_experiment.png").is_file()
    retired = json.loads((output_dir / "retired_candidates.json").read_text(encoding="utf-8"))
    assert retired["candidate_ids"] == ["PLATING-M", "PLATING-H"]
    assert retired["replacement"] == {
        "candidate_id": "PLATING-1P5",
        "scales": [1.0, 1.5, 1.0],
        "resume_policy": "START_FROM_CYCLE_ZERO",
    }
    replacement = json.loads(
        (output_dir / "candidates" / "PLATING-1P5" / "candidate_parameters.json").read_text(
            encoding="utf-8"
        )
    )
    assert replacement["parameters"]["plating_scale"] == 1.5
    resumed = Stage1AgingCalibration(
        RunConfig(data_root=data_root),
        Path(r"E:\SPMe"),
        output_dir,
        CalibrationParameters(calibration_status="CAPACITY_CALIBRATED", capacity_scale_factor=0.95630859375),
        runner_factory=_FakeRunner,
        progress_writer=lambda message: None,
    ).run()
    assert resumed["status"] == result["status"]
    assert resumed["winner"] == result["winner"]
    assert _FakeRunner.calls[:2] == [("BASELINE", 30), ("BASELINE", 75)]
    for candidate in ("SEI-M", "PLATING-1P5", "LAM-M", "SEI-H", "LAM-H"):
        assert (candidate, 25) in _FakeRunner.calls
        assert (candidate, 75) in _FakeRunner.calls
    assert not any(candidate in {"PLATING-M", "PLATING-H"} for candidate, _ in _FakeRunner.calls)


class _IncompleteRptRunner(_FakeRunner):
    calls = []

    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        self.calls.append((output_dir.name, stop_after_cycle))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "checkpoints").mkdir(exist_ok=True)
        (output_dir / "checkpoints" / f"cycle-{stop_after_cycle:03d}.pkl").write_bytes(b"fake")
        with (output_dir / "cycle_summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("cycle",))
            writer.writeheader()
            writer.writerow({"cycle": stop_after_cycle})
        path = output_dir / "rpt_summary.csv"
        complete = len(self.calls) > 1
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("node", "capacity_ah"))
            writer.writeheader()
            writer.writerow({"node": 0, "capacity_ah": 4.89})
            if complete:
                writer.writerow({"node": stop_after_cycle, "capacity_ah": 4.8})
        return RunStatus.PAUSED


def _calibration(workspace_tmp, runner_factory=_FakeRunner) -> Stage1AgingCalibration:
    return Stage1AgingCalibration(
        RunConfig(data_root=workspace_tmp / "data"),
        Path(r"E:\SPMe"),
        workspace_tmp / "stage1",
        CalibrationParameters(calibration_status="CAPACITY_CALIBRATED", capacity_scale_factor=0.95630859375),
        runner_factory=runner_factory,
        progress_writer=lambda message: None,
    )


def test_rpt_checkpoint_filename_does_not_skip_an_incomplete_rpt(workspace_tmp) -> None:
    _IncompleteRptRunner.calls = []
    calibration = _calibration(workspace_tmp, _IncompleteRptRunner)
    candidate = baseline_candidate()

    first, capacities, _ = calibration._run_candidate(candidate, 75, "0" * 64)
    second, capacities, _ = calibration._run_candidate(candidate, 75, "0" * 64)

    assert first is RunStatus.NUMERICAL_FAILURE
    assert second is RunStatus.PAUSED
    assert 75 in capacities
    assert _IncompleteRptRunner.calls == [("BASELINE", 75), ("BASELINE", 75)]


def test_completed_stage_still_enters_runner_resume_preflight(workspace_tmp) -> None:
    _FakeRunner.calls = []
    calibration = _calibration(workspace_tmp, _FakeRunner)
    candidate = baseline_candidate()

    calibration._run_candidate(candidate, 25, "0" * 64)
    calibration._run_candidate(candidate, 25, "0" * 64)

    assert _FakeRunner.calls == [("BASELINE", 25), ("BASELINE", 25)]


def test_validated_completed_stage_is_reused_without_opening_runner(workspace_tmp, monkeypatch) -> None:
    _FakeRunner.calls = []
    calibration = _calibration(workspace_tmp, _FakeRunner)
    candidate = baseline_candidate()
    capacities = {0: 4.9, 25: 4.8}
    checkpoints = calibration.output_dir / "candidates" / candidate.candidate_id / "checkpoints"
    checkpoints.mkdir(parents=True)
    (checkpoints / "cycle-025.pkl").write_bytes(b"validated by test double")
    monkeypatch.setattr(
        calibration,
        "_validated_reusable_candidate",
        lambda selected, stop_cycle: capacities,
    )

    status, observed, retries = calibration._run_candidate(candidate, 25, "0" * 64)

    assert status is RunStatus.PAUSED
    assert observed == capacities
    assert retries == 0
    assert _FakeRunner.calls == []


def test_resume_selects_publicly_committed_checkpoint_not_newer_orphan(workspace_tmp) -> None:
    candidate_dir = workspace_tmp / "candidate"
    checkpoints = candidate_dir / "checkpoints"
    checkpoints.mkdir(parents=True)
    committed = checkpoints / "cycle-025.pkl"
    committed.write_bytes(b"committed")
    (checkpoints / "cycle-075.pkl").write_bytes(b"orphan")
    (candidate_dir / "output_manifest.json").write_text(
        json.dumps({"audit": {"checkpoint": committed.name}, "commit": {}}), encoding="utf-8"
    )

    assert _latest_checkpoint(candidate_dir) == committed


class _BaselineFailureRunner(_FakeRunner):
    calls = []

    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        self.calls.append((output_dir.name, stop_after_cycle))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "checkpoints").mkdir(exist_ok=True)
        (output_dir / "checkpoints" / f"cycle-{stop_after_cycle:03d}.pkl").write_bytes(b"fake")
        return RunStatus.PAUSED


def test_baseline_cycle_30_gate_stops_before_cycle_75(workspace_tmp) -> None:
    _BaselineFailureRunner.calls = []
    data_root = make_w10_data_root(workspace_tmp / "data")
    calibration = _calibration(workspace_tmp, _BaselineFailureRunner)
    calibration.config = RunConfig(data_root=data_root).normalized(Path(r"E:\SPMe"))

    with pytest.raises(AgingCalibrationError, match="baseline cycle 0-30 regression gate"):
        calibration.run()

    assert _BaselineFailureRunner.calls == [("BASELINE", 30)]


class _RetryRunner(_FakeRunner):
    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        with (output_dir / "solver_attempts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"attempt_count": 2}) + "\n")
        return super().run(output_dir, stop_after_cycle=stop_after_cycle, progress_callback=progress_callback, **kwargs)


def test_retry_count_is_the_log_total_not_pre_and_post_totals_added(workspace_tmp) -> None:
    calibration = _calibration(workspace_tmp, _RetryRunner)
    candidate_dir = calibration.output_dir / "candidates" / "BASELINE"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "solver_attempts.jsonl").write_text(json.dumps({"attempt_count": 2}) + "\n", encoding="utf-8")
    candidate = baseline_candidate()

    _, _, retries = calibration._run_candidate(candidate, 30, "0" * 64)

    assert retries == 2


def test_progress_reports_measured_elapsed_speed_and_both_etas(workspace_tmp, monkeypatch) -> None:
    times = iter((100.0, 101.0, 111.0))
    monkeypatch.setattr("pybamm_w10.calibration.aging.monotonic", lambda: next(times))
    calibration = _calibration(workspace_tmp)
    calibration._candidate_scales["SEI-M"] = (3.16, 1.0, 1.0)
    calibration._candidate_residuals_pp["SEI-M"] = 0.25
    calibration._active_stop_cycle = 20
    calibration._write_progress("PROBE", "SEI-M", ProgressState(phase="STANDARD_CHARGE", completed_cycles=0, current_cycle=1))
    calibration._write_progress("PROBE", "SEI-M", ProgressState(phase="STANDARD_CHARGE", completed_cycles=10, current_cycle=11))

    progress = json.loads((calibration.output_dir / "stage1_progress.json").read_text(encoding="utf-8"))
    assert progress["elapsed_wall_clock_s"] == pytest.approx(11.0)
    assert progress["recent_cycle_wall_clock_s"] == pytest.approx(1.0)
    assert progress["current_stage_eta_s"] == pytest.approx(10.0)
    assert progress["full_workflow_eta_s"] > progress["current_stage_eta_s"]
    assert progress["scales"] == [3.16, 1.0, 1.0]
    assert progress["latest_soh_residual_pp"] == pytest.approx(0.25)
    assert progress["estimated_completion_utc"]
    history = (calibration.output_dir / "stage1_progress_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 2


class _WinnerValidationFailureRunner(_FakeRunner):
    calls = []

    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        if output_dir.name == "A" and stop_after_cycle == 350:
            self.calls.append((output_dir.name, stop_after_cycle))
            return RunStatus.NUMERICAL_FAILURE
        status = super().run(output_dir, stop_after_cycle=stop_after_cycle, progress_callback=progress_callback, **kwargs)
        if output_dir.name == "B":
            path = output_dir / "rpt_summary.csv"
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("node", "capacity_ah"))
                writer.writeheader()
                for index, row in enumerate(rows):
                    writer.writerow({"node": row["node"], "capacity_ah": float(row["capacity_ah"]) - 0.002 * index})
        return status


class _UnsafeMidProbeRunner(_FakeRunner):
    calls = []

    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        status = super().run(
            output_dir,
            stop_after_cycle=stop_after_cycle,
            progress_callback=progress_callback,
            **kwargs,
        )
        if output_dir.name == "SEI-M" and stop_after_cycle == 25:
            path = output_dir / "rpt_summary.csv"
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("node", "capacity_ah"))
                writer.writeheader()
                for row in rows:
                    writer.writerow({
                        "node": row["node"],
                        "capacity_ah": 3.0 if int(row["node"]) == 25 else row["capacity_ah"],
                    })
        return status


def test_low_soh_mid_probe_is_audit_only_not_an_unapproved_safety_gate(workspace_tmp) -> None:
    _UnsafeMidProbeRunner.calls = []
    data_root = make_w10_data_root(workspace_tmp / "data")
    calibration = _calibration(workspace_tmp, _UnsafeMidProbeRunner)
    calibration.config = RunConfig(data_root=data_root).normalized(Path(r"E:\SPMe"))

    calibration.run()

    assert ("SEI-M", 25) in _UnsafeMidProbeRunner.calls
    assert ("SEI-M", 75) in _UnsafeMidProbeRunner.calls
    assert ("SEI-H", 25) in _UnsafeMidProbeRunner.calls
    assert ("SEI-H", 75) in _UnsafeMidProbeRunner.calls
    gate = json.loads(
        (calibration.output_dir / "candidates" / "SEI-M" / "cycle25_safety_gate.json").read_text(encoding="utf-8")
    )
    assert gate["passed"] is True
    assert "minimum_soh_pct" not in gate


def test_successful_backup_is_refrozen_and_uses_its_calibration_metrics(workspace_tmp) -> None:
    _WinnerValidationFailureRunner.calls = []
    data_root = make_w10_data_root(workspace_tmp / "data")
    calibration = _calibration(workspace_tmp, _WinnerValidationFailureRunner)
    calibration.config = RunConfig(data_root=data_root).normalized(Path(r"E:\SPMe"))

    result = calibration.run()
    frozen = json.loads((calibration.output_dir / "frozen_parameters.json").read_text(encoding="utf-8"))
    candidates = json.loads((calibration.output_dir / "candidate_manifest.json").read_text(encoding="utf-8"))
    backup = next(item for item in candidates["combinations"] if item["candidate_id"] == "B")

    assert result["winner"] == "A"
    assert result["validated_candidate"] == "B"
    assert result["calibration"]["rmse_pp"] > 0
    assert frozen["parameters"]["sei_scale"] == pytest.approx(backup["scales"][0])


class _AStopsAt122Runner(_FakeRunner):
    calls = []

    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        self.calls.append((output_dir.name, stop_after_cycle))
        if output_dir.name == "A" and stop_after_cycle == 122:
            return RunStatus.NUMERICAL_FAILURE
        return super().run(output_dir, stop_after_cycle=stop_after_cycle, progress_callback=progress_callback, **kwargs)


def test_combination_censored_at_122_is_not_run_to_188(workspace_tmp) -> None:
    _AStopsAt122Runner.calls = []
    data_root = make_w10_data_root(workspace_tmp / "data")
    calibration = _calibration(workspace_tmp, _AStopsAt122Runner)
    calibration.config = RunConfig(data_root=data_root).normalized(Path(r"E:\SPMe"))

    calibration.run()

    assert ("A", 122) in _AStopsAt122Runner.calls
    assert ("A", 188) not in _AStopsAt122Runner.calls


class _PhysicalWinnerValidationRunner(_FakeRunner):
    calls = []

    def run(self, output_dir: Path, *, stop_after_cycle: int, progress_callback, **kwargs) -> RunStatus:
        self.calls.append((output_dir.name, stop_after_cycle))
        if output_dir.name == "A" and stop_after_cycle == 350:
            return RunStatus.PHYSICAL_PROTOCOL_FAILURE
        return super().run(output_dir, stop_after_cycle=stop_after_cycle, progress_callback=progress_callback, **kwargs)


def test_physical_validation_failure_never_starts_backup(workspace_tmp) -> None:
    _PhysicalWinnerValidationRunner.calls = []
    data_root = make_w10_data_root(workspace_tmp / "data")
    calibration = _calibration(workspace_tmp, _PhysicalWinnerValidationRunner)
    calibration.config = RunConfig(data_root=data_root).normalized(Path(r"E:\SPMe"))

    result = calibration.run()

    assert result["status"] == "VALIDATION_FAILED"
    assert ("B", 350) not in _PhysicalWinnerValidationRunner.calls


def test_stage1_rejects_a_capacity_scale_other_than_the_frozen_value(workspace_tmp) -> None:
    calibration = Stage1AgingCalibration(
        RunConfig(data_root=workspace_tmp / "data"),
        Path(r"E:\SPMe"),
        workspace_tmp / "stage1",
        CalibrationParameters(calibration_status="CAPACITY_CALIBRATED", capacity_scale_factor=0.9563),
        runner_factory=_FakeRunner,
        progress_writer=lambda message: None,
    )

    with pytest.raises(AgingCalibrationError, match="capacity_scale_factor"):
        calibration.run()


def test_probe_failure_classes_keep_physical_and_numerical_failures_distinct(workspace_tmp) -> None:
    candidate = baseline_candidate()
    numerical = Stage1AgingCalibration._probe(candidate, {}, RunStatus.NUMERICAL_FAILURE, 1)
    physical = Stage1AgingCalibration._probe(candidate, {}, RunStatus.PHYSICAL_PROTOCOL_FAILURE, 0)

    assert numerical.failure_class == "NUMERICALLY_CENSORED"
    assert physical.failure_class == "PHYSICALLY_INFEASIBLE"


def test_mechanism_trends_are_extracted_and_plotted(workspace_tmp) -> None:
    calibration = _calibration(workspace_tmp)
    candidate_dir = calibration.output_dir / "candidates" / "A"
    candidate_dir.mkdir(parents=True)
    fields = (
        "cycle", "total_sei_loss_ah", "reversible_plated_lithium_ah",
        "dead_lithium_ah", "total_plated_lithium_ah", "negative_lam_pct", "positive_lam_pct",
    )
    with (candidate_dir / "degradation_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(dict(zip(fields, (1, 0.01, 0.002, 0.001, 0.003, 0.1, 0.2), strict=True)))
        writer.writerow(dict(zip(fields, (2, 0.02, 0.003, 0.002, 0.005, 0.2, 0.3), strict=True)))

    result = calibration._write_mechanism_trends(candidate_dir)

    assert result["status"] == "available"
    assert result["series"]["cycle"] == [1, 2]
    assert (calibration.output_dir / result["artifact"]).is_file()
    assert (calibration.output_dir / result["plot"]).is_file()
