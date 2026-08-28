"""Explicit, resumable stage-1 SOH aging calibration orchestration."""

from __future__ import annotations

import csv
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import pickle
import subprocess
from time import monotonic
from typing import Callable

from ..config import RunConfig
from ..output import RunDirectoryBusy, RunDirectoryLock, append_json_line
from ..progress import ProgressState
from ..runner import W10Runner
from ..types import Checkpoint, RunStatus
from .artifacts import write_calibration_csv, write_calibration_json
from .data import ANCHOR_NODES, CALIBRATION_NODES
from .objectives import CandidateScore, assess_stage1, rank_candidates, soh_metrics
from .parameters import CalibrationParameters
from .split import (
    HOLDOUT_NODES,
    calibration_target_inventory,
    load_calibration_capacity_targets,
    load_holdout_capacity_targets,
)
from .surrogate import (
    MECHANISMS,
    PLATING_PROBE_ID,
    PLATING_PROBE_SCALE,
    RETIRED_PLATING_CANDIDATE_IDS,
    AgingCandidate,
    ProbeResponse,
    baseline_candidate,
    choose_representative,
    generate_combination_candidates,
    high_probe_reasons,
    high_rate_candidates,
    mid_rate_candidates,
)
from .workflow import CalibrationState, CalibrationWorkflow


STAGE1_DIRNAME = "w10-stage1-soh-v1"
CAPACITY_SCALE_FACTOR = 0.95630859375
MAX_PLANNED_CANDIDATE_CYCLES = 75 + 3 * 75 + 3 * 75 + 2 * 188 + (350 - 188)
SOURCE_AMENDMENT_PATHS = frozenset(
    {
        "src/pybamm_w10/calibration/aging.py",
        "src/pybamm_w10/charge_efficiency.py",
        "src/pybamm_w10/model.py",
        "src/pybamm_w10/calibration/surrogate.py",
    }
)
MECHANISM_TREND_FIELDS = (
    "cycle",
    "total_sei_loss_ah",
    "reversible_plated_lithium_ah",
    "dead_lithium_ah",
    "total_plated_lithium_ah",
    "negative_lam_pct",
    "positive_lam_pct",
)


class AgingCalibrationError(RuntimeError):
    pass


def _sha256_json(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _source_identity(workspace: Path) -> dict[str, object]:
    paths = sorted((workspace / "src" / "pybamm_w10").rglob("*.py"))
    files = {path.relative_to(workspace).as_posix(): sha256(path.read_bytes()).hexdigest() for path in paths}
    try:
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=workspace, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(subprocess.run(("git", "diff", "--quiet"), cwd=workspace, capture_output=True).returncode)
    except (OSError, subprocess.CalledProcessError):
        head, dirty = None, None
    return {"git_head": head, "git_dirty": dirty, "source_files": files, "source_sha256": _sha256_json(files)}


def _source_amendment_changes(
    prior: dict[str, object], current: dict[str, object]
) -> tuple[str, ...]:
    prior_files = prior.get("source_files")
    current_files = current.get("source_files")
    if not isinstance(prior_files, dict) or not isinstance(current_files, dict):
        raise AgingCalibrationError("stage-1 source identity has no file inventory")
    changed = tuple(
        sorted(
            path
            for path in set(prior_files) | set(current_files)
            if prior_files.get(path) != current_files.get(path)
        )
    )
    unexpected = set(changed) - SOURCE_AMENDMENT_PATHS
    if unexpected:
        raise AgingCalibrationError(
            "source amendment changes non-approved files: " + ", ".join(sorted(unexpected))
        )
    return changed


def _latest_checkpoint(candidate_dir: Path) -> Path | None:
    public_manifest = candidate_dir / "output_manifest.json"
    if public_manifest.is_file():
        try:
            selected = json.loads(public_manifest.read_text(encoding="utf-8")).get("audit", {}).get("checkpoint")
        except (OSError, json.JSONDecodeError) as exc:
            raise AgingCalibrationError(f"candidate output manifest is not readable: {public_manifest}") from exc
        if not isinstance(selected, str) or Path(selected).name != selected:
            raise AgingCalibrationError(f"candidate output manifest has no valid checkpoint pointer: {public_manifest}")
        selected_path = candidate_dir / "checkpoints" / selected
        if not selected_path.is_file():
            raise AgingCalibrationError(f"candidate output manifest selects a missing checkpoint: {selected_path}")
        return selected_path
    paths = sorted((candidate_dir / "checkpoints").glob("cycle-*.pkl")) if candidate_dir.exists() else []
    return paths[-1] if paths else None


def _checkpoint_cycle(path: Path | None) -> int:
    if path is None:
        return -1
    return int(path.stem.rsplit("-", 1)[-1])


def _rpt_capacities(candidate_dir: Path) -> dict[int, float]:
    path = candidate_dir / "rpt_summary.csv"
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {int(row["node"]): float(row["capacity_ah"]) for row in csv.DictReader(handle)}


def _last_completed_cycle_result(candidate_dir: Path) -> int:
    path = candidate_dir / "cycle_summary.csv"
    if not path.is_file():
        return -1
    with path.open(newline="", encoding="utf-8") as handle:
        return max((int(row["cycle"]) for row in csv.DictReader(handle)), default=-1)


def _retry_count(candidate_dir: Path) -> int:
    path = candidate_dir / "solver_attempts.jsonl"
    if not path.is_file():
        return 0
    return sum(
        int(json.loads(line).get("attempt_count", 1)) - 1
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def _hash_file(path: Path, limit: int | None = None) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        remaining = limit
        while remaining is None or remaining > 0:
            chunk = handle.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def _failure_class(status: RunStatus, retries: int = 0) -> str:
    if status is RunStatus.PHYSICAL_PROTOCOL_FAILURE:
        return "PHYSICALLY_INFEASIBLE"
    if status is RunStatus.NUMERICAL_FAILURE:
        return "NUMERICALLY_CENSORED"
    return "RETRY_SUCCESS" if retries else "COMPLETED"


def _stage_complete(
    checkpoint: Path | None,
    last_completed_cycle_result: int,
    capacities: dict[int, float],
    stop_cycle: int,
    rpt_nodes: tuple[int, ...],
) -> bool:
    if _checkpoint_cycle(checkpoint) < stop_cycle or last_completed_cycle_result < stop_cycle:
        return False
    if stop_cycle not in rpt_nodes:
        return True
    capacity = capacities.get(stop_cycle)
    return capacity is not None and math.isfinite(capacity) and capacity > 0


class Stage1AgingCalibration:
    """The one-command workflow; numerical work remains solely in ``W10Runner``."""

    def __init__(
        self,
        config: RunConfig,
        workspace: Path,
        output_dir: Path,
        parameters: CalibrationParameters,
        *,
        runner_factory: Callable[[RunConfig, Path, CalibrationParameters], W10Runner] = W10Runner,
        progress_writer: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config.normalized(workspace)
        self.workspace = workspace.resolve()
        self.output_dir = output_dir.resolve()
        self.parameters = parameters
        self.runner_factory = runner_factory
        self.progress_writer = progress_writer or print
        progress_path = self.output_dir / "stage1_progress.json"
        prior = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.is_file() else {}
        self._prior_elapsed_s = float(prior.get("elapsed_wall_clock_s", 0.0))
        self._cycle_samples_s = [float(value) for value in prior.get("elapsed_cycle_samples_s", [])][-10:]
        self._session_started = monotonic()
        self._cycle_anchors: dict[str, tuple[int, float]] = {}
        self._candidate_cycles: dict[str, int] = {}
        self._candidate_scales: dict[str, tuple[float, float, float]] = {}
        self._candidate_residuals_pp: dict[str, float | None] = {}
        self._experimental_soh: dict[int, float] = {}
        self._active_stop_cycle: int | None = None
        self._planned_total_cycles = MAX_PLANNED_CANDIDATE_CYCLES

    def _write_progress(self, stage: str, candidate: str, state: ProgressState | None = None) -> None:
        now = monotonic()
        if state is not None:
            completed = state.completed_cycles
            previous = self._cycle_anchors.get(candidate)
            if previous is not None and completed > previous[0]:
                seconds_per_cycle = (now - previous[1]) / (completed - previous[0])
                self._cycle_samples_s.extend([seconds_per_cycle] * min(completed - previous[0], 10))
                self._cycle_samples_s = self._cycle_samples_s[-10:]
            if previous is None or completed > previous[0]:
                self._cycle_anchors[candidate] = (completed, now)
            self._candidate_cycles[candidate] = max(completed, self._candidate_cycles.get(candidate, 0))
        completed = self._candidate_cycles.get(candidate, 0)
        recent = None if not self._cycle_samples_s else sum(self._cycle_samples_s) / len(self._cycle_samples_s)
        stage_remaining = None if self._active_stop_cycle is None else max(0, self._active_stop_cycle - completed)
        workflow_remaining = max(0, self._planned_total_cycles - sum(self._candidate_cycles.values()))
        full_eta_s = None if recent is None else recent * workflow_remaining
        payload = {
            "stage": stage,
            "candidate_id": candidate,
            "scales": self._candidate_scales.get(candidate),
            "cycle": None if state is None else state.current_cycle,
            "solver_attempt": None if state is None else state.solver_attempt,
            "solver_profile": None if state is None else state.solver_profile,
            "elapsed_wall_clock_s": self._prior_elapsed_s + now - self._session_started,
            "elapsed_cycle_samples_s": self._cycle_samples_s,
            "recent_cycle_wall_clock_s": recent,
            "current_stage_eta_s": None if recent is None or stage_remaining is None else recent * stage_remaining,
            "full_workflow_eta_s": full_eta_s,
            "estimated_completion_utc": None
            if full_eta_s is None
            else (datetime.now(timezone.utc) + timedelta(seconds=full_eta_s)).isoformat(),
            "latest_soh_residual_pp": self._candidate_residuals_pp.get(candidate),
        }
        write_calibration_json(self.output_dir / "stage1_progress.json", payload)
        append_json_line(self.output_dir / "stage1_progress_history.jsonl", payload)
        self.progress_writer(" | ".join(f"{key}={value}" for key, value in payload.items() if key != "elapsed_cycle_samples_s"))

    def _targets_manifest(self) -> tuple[dict[int, float], dict[str, object]]:
        targets = load_calibration_capacity_targets(self.config.data_root)
        manifest = {
            "anchor_nodes": list(ANCHOR_NODES),
            "calibration_nodes": list(CALIBRATION_NODES),
            "calibration_capacities_ah": {str(key): value for key, value in targets.items()},
            "input_inventory": calibration_target_inventory(self.config.data_root),
        }
        return targets, manifest

    def _candidate_parameters(self, candidate: AgingCandidate) -> CalibrationParameters:
        return replace(
            self.parameters,
            calibration_status="CAPACITY_CALIBRATED",
            sei_scale=candidate.scales[0],
            plating_scale=candidate.scales[1],
            lam_scale=candidate.scales[2],
            degradation_parameter_status="not_calibrated",
        )

    def _validated_reusable_candidate(
        self, candidate: AgingCandidate, stop_cycle: int
    ) -> dict[int, float] | None:
        """Return complete committed capacities without opening the runner."""
        candidate_dir = self.output_dir / "candidates" / candidate.candidate_id
        checkpoint_path = _latest_checkpoint(candidate_dir)
        capacities = _rpt_capacities(candidate_dir)
        if not _stage_complete(
            checkpoint_path,
            _last_completed_cycle_result(candidate_dir),
            capacities,
            stop_cycle,
            self.config.protocol.rpt_nodes,
        ):
            return None
        status_path = candidate_dir / "run_status.json"
        parameter_path = candidate_dir / "candidate_parameters.json"
        manifest_path = candidate_dir / "output_manifest.json"
        run_config_path = candidate_dir / "run_config.json"
        if not all(path.is_file() for path in (status_path, parameter_path, manifest_path, run_config_path)):
            return None
        status = json.loads(status_path.read_text(encoding="utf-8"))
        success = status.get("status") in {RunStatus.PAUSED.value, RunStatus.COMPLETED.value}
        reusable_prefix = (
            status.get("status") in {
                RunStatus.NUMERICAL_FAILURE.value,
                RunStatus.PHYSICAL_PROTOCOL_FAILURE.value,
            }
            and int(status.get("completed_aging_cycles", -1)) >= stop_cycle
        )
        if not success and not reusable_prefix:
            raise AgingCalibrationError(f"complete candidate has a non-success status: {candidate.candidate_id}")
        expected_parameters = self._candidate_parameters(candidate).to_json()
        if json.loads(parameter_path.read_text(encoding="utf-8")) != expected_parameters:
            raise AgingCalibrationError(f"complete candidate parameters differ: {candidate.candidate_id}")
        assert checkpoint_path is not None
        with checkpoint_path.open("rb") as handle:
            checkpoint = pickle.load(handle)
        if not isinstance(checkpoint, Checkpoint):
            raise AgingCalibrationError(f"complete candidate checkpoint type is invalid: {candidate.candidate_id}")
        public = json.loads(manifest_path.read_text(encoding="utf-8"))
        if public.get("audit", {}).get("checkpoint") != checkpoint_path.name:
            raise AgingCalibrationError(f"complete candidate public checkpoint pointer differs: {candidate.candidate_id}")
        if public.get("commit") != asdict(checkpoint.output_manifest):
            raise AgingCalibrationError(f"complete candidate checkpoint commit differs: {candidate.candidate_id}")
        saved_config = json.loads(run_config_path.read_text(encoding="utf-8"))
        if saved_config.get("run_context_fingerprint") != checkpoint.run_context_fingerprint:
            raise AgingCalibrationError(f"complete candidate run context differs: {candidate.candidate_id}")
        for relative, commit in checkpoint.output_manifest.append_files.items():
            path = candidate_dir / relative
            if not path.is_file() or path.stat().st_size < commit.byte_offset:
                raise AgingCalibrationError(f"complete candidate committed output is missing: {relative}")
            if commit.byte_offset and _hash_file(path, commit.byte_offset) != commit.prefix_sha256:
                raise AgingCalibrationError(f"complete candidate committed output was modified: {relative}")
        for relative, commit in checkpoint.output_manifest.artifacts.items():
            path = candidate_dir / relative
            if not path.is_file() or path.stat().st_size != commit.size or _hash_file(path) != commit.sha256:
                raise AgingCalibrationError(f"complete candidate artifact was modified: {relative}")
        return capacities

    def _run_candidate(self, candidate: AgingCandidate, stop_cycle: int, context: str) -> tuple[RunStatus, dict[int, float], int]:
        candidate_dir = self.output_dir / "candidates" / candidate.candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=True)
        parameters = self._candidate_parameters(candidate)
        self._candidate_scales[candidate.candidate_id] = candidate.scales
        self._active_stop_cycle = stop_cycle
        reused = self._validated_reusable_candidate(candidate, stop_cycle)
        if reused is not None:
            completed_cycle = max(0, _checkpoint_cycle(_latest_checkpoint(candidate_dir)))
            self._candidate_cycles[candidate.candidate_id] = max(
                completed_cycle, self._candidate_cycles.get(candidate.candidate_id, 0)
            )
            append_json_line(
                self.output_dir / "reused_candidates.jsonl",
                {
                    "candidate_id": candidate.candidate_id,
                    "stop_cycle": stop_cycle,
                    "checkpoint": _latest_checkpoint(candidate_dir).name,
                    "validation": "public commit, prefixes, artifacts, parameters, status",
                    "reused_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            )
            q0 = reused.get(0)
            common_nodes = sorted(set(reused) & set(self._experimental_soh))
            if q0 is not None and q0 > 0 and common_nodes:
                node = common_nodes[-1]
                self._candidate_residuals_pp[candidate.candidate_id] = 100.0 * reused[node] / q0 - self._experimental_soh[node]
            self._write_progress(
                candidate.stage,
                candidate.candidate_id,
                ProgressState(phase=RunStatus.PAUSED.value, completed_cycles=completed_cycle, current_cycle=completed_cycle),
            )
            return RunStatus.PAUSED, reused, _retry_count(candidate_dir)
        parameter_path = candidate_dir / "candidate_parameters.json"
        if parameter_path.is_file():
            if json.loads(parameter_path.read_text(encoding="utf-8")) != parameters.to_json():
                raise AgingCalibrationError(f"existing candidate parameters differ: {candidate.candidate_id}")
        else:
            write_calibration_json(parameter_path, parameters.to_json())
        saved_context = None
        run_config_path = candidate_dir / "run_config.json"
        if run_config_path.is_file():
            saved_context = json.loads(run_config_path.read_text(encoding="utf-8")).get("run_context_fingerprint")
        candidate_config = replace(
            self.config,
            output_root=candidate_dir,
            calibration_parameters_path=candidate_dir / "candidate_parameters.json",
            run_context_fingerprint=saved_context or context,
        )
        runner = self.runner_factory(candidate_config, self.workspace, parameters)
        checkpoint = _latest_checkpoint(candidate_dir)
        capacities = _rpt_capacities(candidate_dir)
        self._candidate_cycles[candidate.candidate_id] = max(
            0, min(_checkpoint_cycle(checkpoint), stop_cycle), self._candidate_cycles.get(candidate.candidate_id, 0)
        )
        self._write_progress(candidate.stage, candidate.candidate_id)
        status = runner.run(
            candidate_dir,
            resume_checkpoint=checkpoint,
            stop_after_cycle=stop_cycle,
            progress_callback=lambda state: self._write_progress(candidate.stage, candidate.candidate_id, state),
            postprocess_full_soh=False,
        )
        checkpoint = _latest_checkpoint(candidate_dir)
        capacities = _rpt_capacities(candidate_dir)
        common_nodes = sorted(set(capacities) & set(self._experimental_soh))
        if common_nodes and capacities.get(0, 0.0) > 0:
            node = common_nodes[-1]
            self._candidate_residuals_pp[candidate.candidate_id] = (
                100.0 * capacities[node] / capacities[0] - self._experimental_soh[node]
            )
        completed_cycle = max(0, _checkpoint_cycle(checkpoint))
        self._candidate_cycles[candidate.candidate_id] = max(
            completed_cycle, self._candidate_cycles.get(candidate.candidate_id, 0)
        )
        if status in {RunStatus.PAUSED, RunStatus.COMPLETED} and not _stage_complete(
            checkpoint,
            _last_completed_cycle_result(candidate_dir),
            capacities,
            stop_cycle,
            self.config.protocol.rpt_nodes,
        ):
            status = RunStatus.NUMERICAL_FAILURE
        self._write_progress(
            candidate.stage,
            candidate.candidate_id,
            ProgressState(phase=status.value, completed_cycles=completed_cycle, current_cycle=completed_cycle),
        )
        return status, capacities, _retry_count(candidate_dir)

    def _probe_safety_passed(
        self,
        candidate: AgingCandidate,
        capacities: dict[int, float],
        status: RunStatus,
        retries: int = 0,
    ) -> bool:
        q0, q25 = capacities.get(0), capacities.get(25)
        soh_25 = None if q0 is None or q25 is None or q0 <= 0 else 100.0 * q25 / q0
        passed = status in {RunStatus.PAUSED, RunStatus.COMPLETED} and soh_25 is not None and math.isfinite(soh_25)
        write_calibration_json(
            self.output_dir / "candidates" / candidate.candidate_id / "cycle25_safety_gate.json",
            {
                "passed": passed,
                "status": status.value,
                "failure_class": _failure_class(status, retries),
                "soh_25_pct": soh_25,
                "criteria": "successful finite cycle-0/cycle-25 RPT only; no SOH threshold",
            },
        )
        return passed

    @staticmethod
    def _probe(candidate: AgingCandidate, capacities: dict[int, float], status: RunStatus, retries: int) -> ProbeResponse:
        q0 = capacities.get(0)
        return ProbeResponse(
            candidate,
            None if q0 is None or 25 not in capacities else 100.0 * capacities[25] / q0,
            None if q0 is None or 75 not in capacities else 100.0 * capacities[75] / q0,
            status not in {RunStatus.PAUSED, RunStatus.COMPLETED},
            retries,
            _failure_class(status, retries),
        )

    def _write_comparison(self, capacities: dict[int, float], targets: dict[int, float]) -> None:
        nodes = ANCHOR_NODES + CALIBRATION_NODES + HOLDOUT_NODES
        metrics = soh_metrics(
            {node: capacities[node] for node in nodes},
            {node: targets[node] for node in nodes},
            nodes,
        )
        rows = [
            {
                "cycle": item.cycle,
                "partition": "calibration" if item.cycle in ANCHOR_NODES + CALIBRATION_NODES else "holdout",
                "simulated_soh_pct": item.simulated_soh_pct,
                "experimental_soh_pct": item.experimental_soh_pct,
                "signed_error_pp": item.signed_error_pp,
                "absolute_error_pp": item.absolute_error_pp,
            }
            for item in metrics.nodes
        ]
        write_calibration_csv(
            self.output_dir / "stage1_soh_comparison.csv",
            ("cycle", "partition", "simulated_soh_pct", "experimental_soh_pct", "signed_error_pp", "absolute_error_pp"),
            rows,
        )
        import matplotlib.pyplot as pyplot

        figure, axis = pyplot.subplots(figsize=(7, 4))
        axis.plot([row["cycle"] for row in rows], [row["experimental_soh_pct"] for row in rows], "o-", label="experiment")
        axis.plot([row["cycle"] for row in rows], [row["simulated_soh_pct"] for row in rows], "s-", label="SPMe")
        axis.axvline(188, color="0.5", linestyle="--", linewidth=1)
        axis.set(xlabel="cycle", ylabel="SOH (%)")
        axis.legend()
        figure.tight_layout()
        path = self.output_dir / "figures" / "stage1_soh_sim_vs_experiment.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path, dpi=150)
        pyplot.close(figure)

        residual_figure, residual_axis = pyplot.subplots(figsize=(7, 3.5))
        residual_axis.axhline(0.0, color="0.5", linewidth=1)
        residual_axis.axvline(188, color="0.5", linestyle="--", linewidth=1)
        residual_axis.plot(
            [row["cycle"] for row in rows],
            [row["signed_error_pp"] for row in rows],
            "o-",
        )
        residual_axis.set(xlabel="cycle", ylabel="SOH residual (percentage points)")
        residual_figure.tight_layout()
        residual_path = self.output_dir / "figures" / "stage1_soh_residuals.png"
        residual_figure.savefig(residual_path, dpi=150)
        pyplot.close(residual_figure)

    def _write_mechanism_trends(self, candidate_dir: Path) -> dict[str, object]:
        source = candidate_dir / "degradation_summary.csv"
        disclaimer = (
            "SEI, plating and LAM values are model-internal trends, "
            "not unique experimental mechanism contributions."
        )
        if not source.is_file():
            return {"status": "pending", "artifact": None, "plot": None, "series": None, "disclaimer": disclaimer}
        with source.open(newline="", encoding="utf-8") as handle:
            rows = []
            for raw in csv.DictReader(handle):
                try:
                    row = {field: float(raw[field]) for field in MECHANISM_TREND_FIELDS}
                except (KeyError, TypeError, ValueError) as exc:
                    raise AgingCalibrationError(f"invalid mechanism trend row in {source}") from exc
                if not all(math.isfinite(value) for value in row.values()):
                    raise AgingCalibrationError(f"non-finite mechanism trend row in {source}")
                row["cycle"] = int(row["cycle"])
                rows.append(row)
        if not rows:
            raise AgingCalibrationError(f"mechanism trend artifact is empty: {source}")
        rows.sort(key=lambda item: int(item["cycle"]))
        artifact = self.output_dir / "stage1_mechanism_trends.csv"
        write_calibration_csv(artifact, MECHANISM_TREND_FIELDS, rows)

        import matplotlib.pyplot as pyplot

        figure, axes = pyplot.subplots(3, 1, figsize=(8, 8), sharex=True)
        cycles = [row["cycle"] for row in rows]
        axes[0].plot(cycles, [row["total_sei_loss_ah"] for row in rows], label="total SEI")
        axes[0].set_ylabel("SEI loss (Ah)")
        axes[1].plot(cycles, [row["reversible_plated_lithium_ah"] for row in rows], label="reversible")
        axes[1].plot(cycles, [row["dead_lithium_ah"] for row in rows], label="dead")
        axes[1].plot(cycles, [row["total_plated_lithium_ah"] for row in rows], label="total")
        axes[1].set_ylabel("plated Li (Ah)")
        axes[1].legend()
        axes[2].plot(cycles, [row["negative_lam_pct"] for row in rows], label="negative")
        axes[2].plot(cycles, [row["positive_lam_pct"] for row in rows], label="positive")
        axes[2].set(xlabel="cycle", ylabel="LAM (%)")
        axes[2].legend()
        figure.tight_layout()
        plot = self.output_dir / "figures" / "stage1_mechanism_trends.png"
        plot.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(plot, dpi=150)
        pyplot.close(figure)
        return {
            "status": "available",
            "artifact": str(artifact.relative_to(self.output_dir)),
            "source": str(source.relative_to(self.output_dir)),
            "plot": str(plot.relative_to(self.output_dir)),
            "series": {field: [row[field] for row in rows] for field in MECHANISM_TREND_FIELDS},
            "disclaimer": disclaimer,
        }

    def _report_audit(
        self, context: str, target_manifest: dict[str, object], source_identity: dict[str, object]
    ) -> dict[str, object]:
        progress_path = self.output_dir / "stage1_progress.json"
        failure_audit: dict[str, str] = {}
        candidate_manifest_path = self.output_dir / "candidate_manifest.json"
        if candidate_manifest_path.is_file():
            candidate_manifest = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
            for responses in candidate_manifest.get("probes", {}).values():
                for response in responses:
                    candidate_id = response.get("candidate", {}).get("candidate_id")
                    if candidate_id:
                        failure_audit[candidate_id] = response.get("failure_class", "UNKNOWN")
        ranking_path = self.output_dir / "candidate_ranking.csv"
        if ranking_path.is_file():
            with ranking_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    failure_audit[row["candidate_id"]] = row.get("failure_class") or "UNKNOWN"
        return {
            "capacity_scale_factor": CAPACITY_SCALE_FACTOR,
            "run_context_fingerprint": context,
            "target_manifest_sha256": _sha256_json(target_manifest),
            "source_identity": source_identity,
            "artifacts": {
                "candidate_manifest": "candidate_manifest.json",
                "candidate_ranking": "candidate_ranking.csv",
                "baseline_gate": "baseline_cycle30_regression_gate.json",
                "progress": "stage1_progress.json",
                "progress_history": "stage1_progress_history.jsonl",
                "soh_comparison_plot": "figures/stage1_soh_sim_vs_experiment.png",
                "soh_residual_plot": "figures/stage1_soh_residuals.png",
            },
            "progress": None if not progress_path.is_file() else json.loads(progress_path.read_text(encoding="utf-8")),
            "candidate_failure_classes": failure_audit,
            "mechanism_trends": {"status": "pending", "disclaimer": "Model-internal trends only; not unique experimental mechanism contributions."},
        }

    def run(self) -> dict[str, object]:
        if self.parameters.degradation_parameter_status != "not_calibrated":
            raise AgingCalibrationError("stage-1 input must have uncalibrated degradation scales")
        if self.parameters.capacity_scale_factor != CAPACITY_SCALE_FACTOR:
            raise AgingCalibrationError(f"stage-1 requires capacity_scale_factor={CAPACITY_SCALE_FACTOR}")
        stage_manifest_path = self.output_dir / "stage1_manifest.json"
        targets, target_manifest = self._targets_manifest()
        source_identity = _source_identity(self.workspace)
        context = _sha256_json(
            {
                "config": self.config.fingerprint(),
                "targets": target_manifest,
                "parameters": self.parameters.fingerprint,
                "source": source_identity,
            }
        )
        with RunDirectoryLock(self.output_dir, {"kind": "stage1_soh_calibration"}):
            manifest = {
                "run_context_fingerprint": context,
                "target_manifest": target_manifest,
                "source_identity": source_identity,
                "candidates": [],
            }
            if stage_manifest_path.is_file():
                prior = json.loads(stage_manifest_path.read_text(encoding="utf-8"))
                if prior.get("run_context_fingerprint") != context:
                    if prior.get("target_manifest") != target_manifest:
                        raise AgingCalibrationError("existing stage-1 target manifest differs")
                    changed = _source_amendment_changes(
                        prior.get("source_identity", {}), source_identity
                    )
                    compatibility_path = self.output_dir / "resume_compatibility.json"
                    if not compatibility_path.is_file():
                        raise AgingCalibrationError(
                            "source amendment requires a passed resume_compatibility.json"
                        )
                    compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
                    if (
                        compatibility.get("status") != "PASSED"
                        or compatibility.get("original_run_context_fingerprint")
                        != prior.get("run_context_fingerprint")
                        or compatibility.get("current_source_sha256")
                        != source_identity.get("source_sha256")
                        or tuple(compatibility.get("changed_source_files", ())) != changed
                    ):
                        raise AgingCalibrationError("resume compatibility audit does not match this source amendment")
                    write_calibration_json(
                        self.output_dir / "resume_amendment.json",
                        {
                            "original_run_context_fingerprint": prior.get("run_context_fingerprint"),
                            "amended_run_context_fingerprint": context,
                            "original_source_sha256": prior.get("source_identity", {}).get("source_sha256"),
                            "current_source_sha256": source_identity.get("source_sha256"),
                            "changed_source_files": list(changed),
                            "compatibility_audit": compatibility_path.name,
                            "completed_candidates_reused_read_only": ["BASELINE", "SEI-M", "LAM-M", "LAM-H"],
                            "failed_candidates_resumed_from_committed_prefix": {
                                "SEI-H": "cycle-032.pkl",
                            },
                            "retired_candidates_excluded": list(RETIRED_PLATING_CANDIDATE_IDS),
                            "new_candidates_start_from_cycle_zero": {
                                PLATING_PROBE_ID: [1.0, PLATING_PROBE_SCALE, 1.0],
                            },
                        },
                    )
                report_path = self.output_dir / "stage1_report.json"
                if report_path.is_file():
                    return json.loads(report_path.read_text(encoding="utf-8"))
            else:
                unexpected = {
                    path.name
                    for path in self.output_dir.iterdir()
                    if path.name not in {".run.lock", "target_manifest.json", "stage1_progress.json"}
                }
                if unexpected:
                    raise AgingCalibrationError("refusing unknown non-empty stage-1 output directory")
                target_path = self.output_dir / "target_manifest.json"
                if target_path.is_file() and json.loads(target_path.read_text(encoding="utf-8")) != target_manifest:
                    raise AgingCalibrationError("existing target manifest does not match current calibration targets")
                write_calibration_json(stage_manifest_path, manifest)
            write_calibration_json(self.output_dir / "target_manifest.json", target_manifest)
            write_calibration_json(
                self.output_dir / "retired_candidates.json",
                {
                    "status": "EXCLUDED_FROM_STAGE1_FIT",
                    "candidate_ids": list(RETIRED_PLATING_CANDIDATE_IDS),
                    "parameter_scales": {
                        "PLATING-M": [1.0, 3.16, 1.0],
                        "PLATING-H": [1.0, 10.0, 1.0],
                    },
                    "replacement": {
                        "candidate_id": PLATING_PROBE_ID,
                        "scales": [1.0, PLATING_PROBE_SCALE, 1.0],
                        "resume_policy": "START_FROM_CYCLE_ZERO",
                    },
                    "old_directories_retained_read_only_for_audit": True,
                },
            )
            status_path = self.output_dir / "stage1_status.json"
            workflow = (
                CalibrationWorkflow.resume(self.output_dir, status_path=status_path, parameter_fingerprint=self.parameters.fingerprint)
                if status_path.is_file()
                else CalibrationWorkflow(self.output_dir, self.parameters.fingerprint, state=CalibrationState.AGING_CALIBRATION_READY, history=[CalibrationState.AGING_CALIBRATION_READY.value], status_path=status_path)
            )
            experimental_soh = {cycle: 100.0 * capacity / targets[0] for cycle, capacity in targets.items()}
            self._experimental_soh = experimental_soh
            if workflow.state is CalibrationState.AGING_CALIBRATION_READY:
                workflow.transition(CalibrationState.PROBING)
            baseline = baseline_candidate()
            baseline_gate_status, baseline_caps, baseline_retries = self._run_candidate(baseline, 30, context)
            baseline_dir = self.output_dir / "candidates" / baseline.candidate_id
            baseline_checkpoint = _latest_checkpoint(baseline_dir)
            baseline_completed_result = _last_completed_cycle_result(baseline_dir)
            baseline_gate_passed = (
                baseline_gate_status in {RunStatus.PAUSED, RunStatus.COMPLETED}
                and _checkpoint_cycle(baseline_checkpoint) >= 30
                and baseline_completed_result >= 30
            )
            write_calibration_json(
                self.output_dir / "baseline_cycle30_regression_gate.json",
                {
                    "passed": baseline_gate_passed,
                    "status": baseline_gate_status.value,
                    "completed_cycle": _checkpoint_cycle(baseline_checkpoint),
                    "last_completed_cycle_result": baseline_completed_result,
                    "failure_class": _failure_class(baseline_gate_status, baseline_retries),
                    "formal_numerical_gate": True,
                    "resume_validation_entrypoint": "W10Runner",
                },
            )
            if not baseline_gate_passed:
                raise AgingCalibrationError("baseline cycle 0-30 regression gate failed")
            baseline_status, baseline_caps, baseline_retries = self._run_candidate(baseline, 75, context)
            baseline_probe = self._probe(baseline, baseline_caps, baseline_status, baseline_retries)
            probes: dict[str, list[ProbeResponse]] = {name: [] for name in MECHANISMS}
            for mechanism, candidate in zip(MECHANISMS, mid_rate_candidates(), strict=True):
                status, capacities, retries = self._run_candidate(candidate, 25, context)
                if self._probe_safety_passed(candidate, capacities, status, retries):
                    status, capacities, retries = self._run_candidate(candidate, 75, context)
                    probes[mechanism].append(self._probe(candidate, capacities, status, retries))
                else:
                    self._planned_total_cycles -= 50
                    probes[mechanism].append(
                        ProbeResponse(candidate, None, None, True, retries, _failure_class(status, retries))
                    )
            high_candidates = high_rate_candidates(
                baseline_probe, {name: values[0] for name, values in probes.items()}, experimental_soh
            )
            self._planned_total_cycles -= (len(MECHANISMS) - len(high_candidates)) * 75
            for candidate in high_candidates:
                mechanism = candidate.candidate_id.split("-")[0].lower()
                status, capacities, retries = self._run_candidate(candidate, 25, context)
                if self._probe_safety_passed(candidate, capacities, status, retries):
                    status, capacities, retries = self._run_candidate(candidate, 75, context)
                    probes[mechanism].append(self._probe(candidate, capacities, status, retries))
                else:
                    self._planned_total_cycles -= 50
                    probes[mechanism].append(
                        ProbeResponse(candidate, None, None, True, retries, _failure_class(status, retries))
                    )
            representatives = choose_representative(
                {name: tuple(values) for name, values in probes.items()}, baseline_probe, experimental_soh[75]
            )
            proposal = generate_combination_candidates(baseline_probe, representatives, experimental_soh)
            if workflow.state is CalibrationState.PROBING:
                workflow.transition(CalibrationState.COMBINATIONS_PROPOSED)
            write_calibration_json(
                self.output_dir / "candidate_manifest.json",
                {
                    "baseline": asdict(baseline),
                    "baseline_probe": asdict(baseline_probe),
                    "probes": {key: [asdict(item) for item in value] for key, value in probes.items()},
                    "high_probe_candidates": [asdict(item) for item in high_candidates],
                    "retired_candidate_ids": list(RETIRED_PLATING_CANDIDATE_IDS),
                    "fixed_plating_probe": {
                        "candidate_id": PLATING_PROBE_ID,
                        "scales": [1.0, PLATING_PROBE_SCALE, 1.0],
                        "high_probe_disabled": True,
                    },
                    "high_probe_reasons": {
                        name: list(high_probe_reasons(baseline_probe, values[0], experimental_soh))
                        for name, values in probes.items()
                    },
                    "representatives": {key: asdict(value) for key, value in representatives.items()},
                    "combinations": [asdict(proposal.candidate_a), asdict(proposal.candidate_b)],
                    "predicted_rmse_a_pp": proposal.predicted_rmse_a_pp,
                    "predicted_rmse_b_pp": proposal.predicted_rmse_b_pp,
                    "diversity_reason": proposal.diversity_reason,
                },
            )
            results: list[CandidateScore] = []
            combination_status: dict[str, RunStatus] = {}
            combination_failure_class: dict[str, str] = {}
            for stop in (122, 188):
                for candidate in (proposal.candidate_a, proposal.candidate_b):
                    if stop == 188 and combination_status.get(candidate.candidate_id) not in {
                        RunStatus.PAUSED,
                        RunStatus.COMPLETED,
                    }:
                        results.append(CandidateScore(candidate.candidate_id, None, candidate.log10_scales, 0, True))
                        continue
                    status, capacities, retries = self._run_candidate(candidate, stop, context)
                    combination_status[candidate.candidate_id] = status
                    combination_failure_class[candidate.candidate_id] = _failure_class(status, retries)
                    if stop == 188:
                        metric = None if status not in {RunStatus.PAUSED, RunStatus.COMPLETED} else soh_metrics(
                            {node: capacities[node] for node in ANCHOR_NODES + CALIBRATION_NODES},
                            targets,
                            ANCHOR_NODES + CALIBRATION_NODES,
                        )
                        results.append(CandidateScore(candidate.candidate_id, metric, candidate.log10_scales, retries, metric is None))
            ranked = rank_candidates(results)
            ranks = {item.candidate_id: index for index, item in enumerate(ranked, start=1)}
            write_calibration_csv(
                self.output_dir / "candidate_ranking.csv",
                (
                    "rank", "candidate_id", "rmse_pp", "max_absolute_error_pp", "endpoint_absolute_error_pp",
                    "log10_norm", "retry_count", "numerically_censored", "failure_class", "stage_status",
                ),
                [
                    {
                        "rank": ranks.get(item.candidate_id),
                        "candidate_id": item.candidate_id,
                        "rmse_pp": None if item.metrics is None else item.metrics.rmse_pp,
                        "max_absolute_error_pp": None if item.metrics is None else item.metrics.max_absolute_error_pp,
                        "endpoint_absolute_error_pp": None if item.metrics is None else item.metrics.endpoint_absolute_error_pp,
                        "log10_norm": math.sqrt(sum(value * value for value in item.log10_scales)),
                        "retry_count": item.retry_count,
                        "numerically_censored": item.numerically_censored,
                        "failure_class": combination_failure_class.get(item.candidate_id, "NUMERICALLY_CENSORED"),
                        "stage_status": combination_status.get(item.candidate_id, RunStatus.NUMERICAL_FAILURE).value,
                    }
                    for item in ranked + tuple(item for item in results if item.candidate_id not in ranks)
                ],
            )
            if workflow.state is CalibrationState.COMBINATIONS_PROPOSED:
                workflow.transition(CalibrationState.SPME_CALIBRATED)
            if not ranked or not assess_stage1(ranked[0].metrics).calibration_passed:
                workflow.transition(CalibrationState.CALIBRATION_FAILED)
                report = {
                    "status": CalibrationState.CALIBRATION_FAILED.value,
                    "ranking": [item.candidate_id for item in ranked],
                    **self._report_audit(context, target_manifest, source_identity),
                }
                write_calibration_json(self.output_dir / "stage1_report.json", report)
                return report
            winner = next(candidate for candidate in (proposal.candidate_a, proposal.candidate_b) if candidate.candidate_id == ranked[0].candidate_id)
            frozen = replace(self._candidate_parameters(winner), calibration_status="PARAMETERS_FROZEN", degradation_parameter_status="soh_stage1_calibrated")
            frozen_path = self.output_dir / "frozen_parameters.json"
            write_calibration_json(frozen_path, frozen.to_json())
            frozen_hash = sha256(frozen_path.read_bytes()).hexdigest()
            if workflow.state is CalibrationState.SPME_CALIBRATED:
                workflow.transition(CalibrationState.PARAMETERS_FROZEN)
            if workflow.state is CalibrationState.PARAMETERS_FROZEN:
                workflow.transition(CalibrationState.VALIDATING)
            validated_candidate = winner
            calibration_score = ranked[0]
            status, capacities, _ = self._run_candidate(winner, 350, context)
            if status not in {RunStatus.PAUSED, RunStatus.COMPLETED}:
                if status is RunStatus.PHYSICAL_PROTOCOL_FAILURE:
                    workflow.transition(CalibrationState.VALIDATION_FAILED, reason="winner physical protocol failure")
                    report = {
                        "status": CalibrationState.VALIDATION_FAILED.value,
                        "winner": winner.candidate_id,
                        "validated_candidate": None,
                        "validation_failure_status": status.value,
                        "backup_not_run_reason": "physical failures must not trigger backup",
                        **self._report_audit(context, target_manifest, source_identity),
                    }
                    write_calibration_json(self.output_dir / "stage1_report.json", report)
                    return report
                backup_score = ranked[1] if len(ranked) > 1 else None
                if backup_score is None or not assess_stage1(backup_score.metrics).calibration_passed:
                    workflow.transition(CalibrationState.VALIDATION_NUMERICAL_FAILURE)
                    report = {
                        "status": CalibrationState.VALIDATION_NUMERICAL_FAILURE.value,
                        "winner": winner.candidate_id,
                        **self._report_audit(context, target_manifest, source_identity),
                    }
                    write_calibration_json(self.output_dir / "stage1_report.json", report)
                    return report
                backup = next(candidate for candidate in (proposal.candidate_a, proposal.candidate_b) if candidate.candidate_id == backup_score.candidate_id)
                self._planned_total_cycles += 350 - 188
                status, capacities, _ = self._run_candidate(backup, 350, context)
                if status not in {RunStatus.PAUSED, RunStatus.COMPLETED}:
                    workflow.transition(CalibrationState.VALIDATION_NUMERICAL_FAILURE)
                    report = {
                        "status": CalibrationState.VALIDATION_NUMERICAL_FAILURE.value,
                        "winner": winner.candidate_id,
                        "backup": backup.candidate_id,
                        **self._report_audit(context, target_manifest, source_identity),
                    }
                    write_calibration_json(self.output_dir / "stage1_report.json", report)
                    return report
                validated_candidate = backup
                calibration_score = backup_score
                frozen = replace(self._candidate_parameters(backup), calibration_status="PARAMETERS_FROZEN", degradation_parameter_status="soh_stage1_calibrated")
                write_calibration_json(frozen_path, frozen.to_json())
                frozen_hash = sha256(frozen_path.read_bytes()).hexdigest()
            holdout = load_holdout_capacity_targets(self.config.data_root, parameter_status="PARAMETERS_FROZEN", frozen_parameters_hash=frozen_hash, frozen_parameters_path=frozen_path, audit_path=self.output_dir / "holdout_access.json")
            all_targets = {**targets, **holdout}
            validation = soh_metrics({node: capacities[node] for node in ANCHOR_NODES + HOLDOUT_NODES}, {node: all_targets[node] for node in ANCHOR_NODES + HOLDOUT_NODES}, ANCHOR_NODES + HOLDOUT_NODES)
            self._write_comparison(capacities, all_targets)
            acceptance = assess_stage1(calibration_score.metrics, validation)
            workflow.transition(CalibrationState.HOLDOUT_EVALUATED)
            final_state = CalibrationState.COMPLETED if acceptance.holdout_passed and acceptance.cycle_350_passed else CalibrationState.VALIDATION_FAILED
            workflow.transition(final_state)
            candidate_paths = {
                candidate.candidate_id: {
                    "checkpoint": None
                    if _latest_checkpoint(self.output_dir / "candidates" / candidate.candidate_id) is None
                    else str(_latest_checkpoint(self.output_dir / "candidates" / candidate.candidate_id).relative_to(self.output_dir)),
                    "scales": candidate.scales,
                }
                for candidate in (proposal.candidate_a, proposal.candidate_b)
            }
            mechanism_trends = self._write_mechanism_trends(
                self.output_dir / "candidates" / validated_candidate.candidate_id
            )
            report = {
                "status": final_state.value,
                "winner": winner.candidate_id,
                "validated_candidate": validated_candidate.candidate_id,
                "backup": None if len(ranked) < 2 else ranked[1].candidate_id,
                **self._report_audit(context, target_manifest, source_identity),
                "capacity_scale_factor": CAPACITY_SCALE_FACTOR,
                "candidates": candidate_paths,
                "calibration": asdict(calibration_score.metrics),
                "holdout": asdict(validation),
                "acceptance": asdict(acceptance),
                "frozen_parameters_sha256": frozen_hash,
                "run_context_fingerprint": context,
                "target_manifest_sha256": _sha256_json(target_manifest),
                "source_identity": source_identity,
                "baseline_cycle30_gate": json.loads((self.output_dir / "baseline_cycle30_regression_gate.json").read_text(encoding="utf-8")),
                "progress": json.loads((self.output_dir / "stage1_progress.json").read_text(encoding="utf-8")),
                "mechanism_trends": mechanism_trends,
            }
            write_calibration_json(self.output_dir / "stage1_report.json", report)
            return report
