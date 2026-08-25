"""PyBaMM execution adapter and reproducible canonical initial state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import platform
import re
import sys
from time import monotonic
from typing import Any, Callable

import numpy as np

from .config import RunConfig
from .charge_variables import ResolvedChargeVariables
from .model import (
    ModelArtifacts,
    build_solver,
    certified_charge_solver_profile,
    conservative_charge_solver_profile,
)
from .types import (
    InitialStateRecord,
    ChargeStageTrace,
    ChargeStateSnapshot,
    FailureContext,
    FailureReason,
    NumericalFailure,
    PhysicalProtocolFailure,
    ProtocolPhase,
    StageOutcome,
    StageSpec,
    SolverAttemptFailure,
    SolverProfile,
    SolverStepFailure,
    StandardChargeSequenceResult,
    TerminationKind,
)
from .udds import CurrentProfile


STANDARD_CHARGE_STAGE_NAMES = ("3c_cc", "4v_cv", "c4_cc", "4p2v_cv")


def build_standard_charge_experiment(config: RunConfig) -> Any:
    """Build the unchanged four-stage charge as one PyBaMM cycle."""
    import pybamm

    protocol = config.protocol
    return pybamm.Experiment([
        (
            pybamm.step.current(-protocol.charge_3c_a, termination="4.0 V"),
            pybamm.step.voltage(4.0, termination=f"{protocol.cv_cutoff_a} A"),
            pybamm.step.current(
                -protocol.discharge_c4_a,
                termination=f"{config.cell.upper_cutoff_v} V",
            ),
            pybamm.step.voltage(
                config.cell.upper_cutoff_v,
                termination=f"{protocol.cv_cutoff_a} A",
            ),
        )
    ])


def _hash_json(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _registered_event_matches(raw_termination: str, event_name: str) -> bool:
    """Match only complete, stage-registered event names."""
    raw = raw_termination.strip().casefold()
    event = event_name.strip().casefold()
    registered = {event, f"event: {event}", f"termination: {event}"}
    # PyBaMM expands the protocol's registered shorthand (``4.0 V`` or
    # ``0.05 A``) to a directional, unit-bracketed experiment event. Enumerate
    # those exact renderings rather than using a broad voltage/current substring.
    scalar = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([va])", event)
    if scalar:
        value, unit = scalar.groups()
        label = "voltage" if unit == "v" else "current"
        bracket = "v" if unit == "v" else "a"
        for direction in (">", "<"):
            rendered = f"{label} {direction} {value} [{bracket}]"
            registered.update({rendered, f"event: {rendered}", f"termination: {rendered}"})
        if unit == "a":
            rendered = f"abs(current [a]) < {value} [a]"
            registered.update({rendered, f"event: {rendered}", f"termination: {rendered}"})
    # PyBaMM appends this exact provenance suffix to custom experiment events.
    return raw in registered or raw.removesuffix(" [experiment]") in registered


def map_termination(raw_termination: str | None, spec: StageSpec) -> TerminationKind:
    """Map a PyBaMM termination using the current stage's declared events only."""
    if raw_termination is None:
        raise ValueError("raw termination text is required")
    raw = raw_termination.strip()
    if raw.casefold() == "final time":
        return TerminationKind.FINAL_TIME
    if any(_registered_event_matches(raw, name) for name in spec.expected_event_names):
        return spec.expected_termination
    for name in spec.allowed_physical_event_names:
        if _registered_event_matches(raw, name):
            if (
                TerminationKind.VOLTAGE in spec.allowed_physical_terminations
                and name.strip().casefold().endswith(" v")
            ):
                return TerminationKind.VOLTAGE
            if TerminationKind.MODEL_PHYSICAL_EVENT in spec.allowed_physical_terminations:
                return TerminationKind.MODEL_PHYSICAL_EVENT
    return TerminationKind.UNKNOWN


def _stable_parameter_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return {
            "dtype": str(value.dtype),
            "shape": list(value.shape),
            "sha256": sha256(np.ascontiguousarray(value).tobytes()).hexdigest(),
        }
    if isinstance(value, (list, tuple)):
        return [_stable_parameter_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_parameter_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if callable(value):
        return {
            "callable_module": getattr(value, "__module__", type(value).__module__),
            "callable_qualname": getattr(value, "__qualname__", type(value).__qualname__),
        }
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}", "value": str(value)}


def _mesh_hash(mesh: Any) -> str:
    digest = sha256()
    for domain, submeshes in sorted(mesh.items(), key=lambda item: repr(item[0])):
        digest.update(repr(domain).encode("utf-8"))
        members = submeshes if isinstance(submeshes, list) else [submeshes]
        for submesh in members:
            for name in ("nodes", "edges"):
                values = getattr(submesh, name, None)
                if values is not None:
                    digest.update(name.encode("ascii"))
                    digest.update(np.ascontiguousarray(values).tobytes())
    return digest.hexdigest()


def construct_initial_state_record(artifacts: ModelArtifacts, config: RunConfig) -> InitialStateRecord:
    """Build, but do not solve, the explicitly configured initial-SOC state."""
    import casadi
    import pybamm

    simulation = pybamm.Simulation(
        artifacts.model,
        parameter_values=artifacts.parameter_values,
        solver=artifacts.solver,
    )
    simulation.build(initial_soc=config.initial_soc)
    state = np.ascontiguousarray(simulation.built_model.concatenated_initial_conditions.entries)
    if not np.all(np.isfinite(state)):
        raise ValueError("canonical initial state contains NaN or Inf")
    parameters = simulation.parameter_values
    neg_initial = float(parameters["Initial concentration in negative electrode [mol.m-3]"])
    pos_initial = float(parameters["Initial concentration in positive electrode [mol.m-3]"])
    neg_max = float(parameters["Maximum concentration in negative electrode [mol.m-3]"])
    pos_max = float(parameters["Maximum concentration in positive electrode [mol.m-3]"])
    parameter_payload = [
        (str(key), _stable_parameter_value(value)) for key, value in sorted(parameters.items())
    ]
    model_payload = {
        "name": artifacts.model.name,
        "options": dict(artifacts.options),
        "rhs": sorted(str(key) for key in simulation.built_model.rhs),
        "algebraic": sorted(str(key) for key in simulation.built_model.algebraic),
        "state_size": int(state.size),
    }
    return InitialStateRecord(
        initial_soc=config.initial_soc,
        method="pybamm.Simulation.build(initial_soc=...)",
        method_arguments={"initial_soc": config.initial_soc, "direction": None, "inputs": None},
        state_size=int(state.size),
        state_sha256=sha256(state.tobytes()).hexdigest(),
        model_sha256=_hash_json(model_payload),
        parameter_sha256=_hash_json(parameter_payload),
        mesh_sha256=_mesh_hash(simulation.mesh),
        main_time_s=0.0,
        calendar_time_s=0.0,
        discharge_capacity_ah=0.0,
        negative_stoichiometry=neg_initial / neg_max,
        positive_stoichiometry=pos_initial / pos_max,
        versions={
            "python": platform.python_version(),
            "pybamm": pybamm.__version__,
            "casadi": casadi.__version__,
            "platform": sys.platform,
        },
    )


def _terminal_state_vector(solution: Any) -> np.ndarray:
    """Return only the final state, even when PyBaMM cannot concatenate history.

    Consecutive experiment steps can legitimately rebuild a discretisation
    after a geometry-scaled parameter update.  PyBaMM then rejects ``solution.y``
    because it represents every historical step, while ``last_state.y`` remains
    the valid terminal state needed for an audit hash.
    """
    terminal = getattr(solution, "last_state", solution)
    return np.ascontiguousarray(terminal.y[:, -1])


def _rebase_terminal_solution(solution: Any | None) -> Any | None:
    """Return the identical terminal numerical state with a local time of zero."""
    if solution is None:
        return None
    import pybamm

    terminal = getattr(solution, "last_state", solution)
    y = np.asarray(terminal.all_ys[0])[:, -1:]
    yp = None
    if getattr(terminal, "all_yps", None) is not None:
        yp = np.asarray(terminal.all_yps[0])[:, -1:]
    local = pybamm.Solution(
        np.asarray([0.0]),
        y,
        terminal.all_models[0],
        terminal.all_inputs[0],
        termination="final time",
        all_yps=yp,
        options=getattr(terminal, "user_options", None),
    )
    local.solve_time = 0
    local.integration_time = 0
    local.set_up_time = 0
    if not np.array_equal(_terminal_state_vector(local), _terminal_state_vector(terminal)):
        raise ValueError("local-time rebase changed the terminal state")
    return local


@dataclass(frozen=True)
class _CommittedSegment:
    global_start_s: float
    global_end_s: float
    local_solution: Any


@dataclass(frozen=True, eq=False)
class PyBaMMSnapshot:
    solution: Any
    time_s: float
    calendar_time_s: float
    discharge_capacity_ah: float
    state_hash: str

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, PyBaMMSnapshot)
            and self.time_s == other.time_s
            and self.calendar_time_s == other.calendar_time_s
            and self.discharge_capacity_ah == other.discharge_capacity_ah
            and self.state_hash == other.state_hash
        )


class PyBaMMBackend:
    """Map protocol primitives to one-step PyBaMM experiments with restart state."""

    def __init__(
        self,
        artifacts: ModelArtifacts,
        initial_soc: float = 0.20,
        initial_state: InitialStateRecord | None = None,
    ):
        self.artifacts = artifacts
        self.initial_soc = initial_soc
        self.initial_state = initial_state
        self.solution: Any | None = None
        self.last_termination: str | None = None
        self.last_termination_time_s: float | None = None
        self.last_termination_value: float | None = None
        self.last_outcome: StageOutcome | None = None
        self._elapsed_time_s = 0.0
        self._committed_segments: list[_CommittedSegment] = []

    def snapshot(self) -> PyBaMMSnapshot:
        if self.solution is None:
            state_hash = self.initial_state.state_sha256 if self.initial_state else "unbuilt-initial"
            return PyBaMMSnapshot(None, self._elapsed_time_s, self._elapsed_time_s, 0.0, state_hash)
        y = _terminal_state_vector(self.solution)
        return PyBaMMSnapshot(
            _rebase_terminal_solution(self.solution),
            self._elapsed_time_s,
            self._elapsed_time_s,
            self.discharge_capacity_ah(),
            sha256(y.tobytes()).hexdigest(),
        )

    def compact_state(self) -> None:
        """Discard historical samples after their summaries/traces are committed."""
        if self.solution is not None:
            self.solution = _rebase_terminal_solution(self.solution)
        self._committed_segments = []

    def fork(self) -> "PyBaMMBackend":
        branch = PyBaMMBackend(self.artifacts, self.initial_soc, self.initial_state)
        branch.restore(self.snapshot())
        return branch

    def restore(self, state: PyBaMMSnapshot) -> None:
        self.solution = state.solution
        self._elapsed_time_s = float(state.time_s)
        self._committed_segments = []
        self.last_termination = None
        self.last_termination_time_s = None
        self.last_termination_value = None
        self.last_outcome = None

    def current_time_s(self) -> float:
        return self._elapsed_time_s

    def calendar_time_s(self) -> float:
        return self._elapsed_time_s

    def discharge_capacity_ah(self) -> float:
        if self.solution is None:
            return 0.0
        return float(self.solution["Discharge capacity [A.h]"](self.solution.t[-1]))

    def _commit_local_candidate(self, candidate: Any, global_start_s: float) -> float:
        local_start_s = float(candidate.t[0])
        local_end_s = float(candidate.t[-1])
        duration_s = local_end_s - local_start_s
        if not np.isfinite(duration_s) or duration_s < 0:
            raise NumericalFailure(FailureContext(
                reason=FailureReason.INVALID_STATE,
                message="local candidate has an invalid duration",
            ))
        global_end_s = float(global_start_s) + duration_s
        self.solution = candidate
        self._elapsed_time_s = global_end_s
        self._committed_segments.append(
            _CommittedSegment(float(global_start_s), global_end_s, candidate)
        )
        return global_end_s

    def _run(self, step: Any, spec: StageSpec, *, solver: Any | None = None) -> StageOutcome:
        import pybamm

        committed = self.snapshot()
        simulation = pybamm.Simulation(
            self.artifacts.model,
            parameter_values=self.artifacts.parameter_values,
            solver=self.artifacts.solver if solver is None else solver,
            experiment=pybamm.Experiment([step]),
        )
        kwargs: dict[str, Any] = {
            "starting_solution": committed.solution,
            "showprogress": False,
        }
        if committed.solution is None:
            kwargs["initial_soc"] = self.initial_soc
        try:
            candidate = simulation.solve(**kwargs)
        except Exception as exc:
            raise RuntimeError(f"PyBaMM solver failed in protocol step {step!r}") from exc
        local_outcome = self._stage_outcome_from_solution(candidate, spec, 0.0)
        if local_outcome.termination_kind is not spec.expected_termination:
            context = FailureContext(
                reason=(
                    FailureReason.PHYSICAL_EVENT_BEFORE_TARGET
                    if local_outcome.termination_kind in spec.allowed_physical_terminations
                    else FailureReason.UNEXPECTED_FINAL_TIME
                    if local_outcome.termination_kind is TerminationKind.FINAL_TIME
                    else FailureReason.UNKNOWN_TERMINATION
                ),
                phase=spec.phase,
                termination_kind=local_outcome.termination_kind,
                raw_termination=local_outcome.raw_termination,
                message="local candidate did not reach the expected protocol event",
            )
            if local_outcome.termination_kind in spec.allowed_physical_terminations:
                raise PhysicalProtocolFailure(context)
            raise NumericalFailure(context)
        global_end_s = self._commit_local_candidate(candidate, committed.time_s)
        self.last_termination = local_outcome.raw_termination
        self.last_termination_time_s = global_end_s
        self.last_outcome = replace(local_outcome, termination_time_s=global_end_s)
        return self.last_outcome

    @staticmethod
    def _standard_charge_specs(config: RunConfig, model_events: tuple[str, ...]) -> tuple[StageSpec, ...]:
        return (
            StageSpec(
                ProtocolPhase.STANDARD_CHARGE,
                TerminationKind.VOLTAGE,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
                expected_event_names=("4.0 V",),
                allowed_physical_event_names=model_events,
            ),
            StageSpec(
                ProtocolPhase.STANDARD_CHARGE,
                TerminationKind.CURRENT,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
                expected_event_names=(f"{config.protocol.cv_cutoff_a} A",),
                allowed_physical_event_names=model_events,
            ),
            StageSpec(
                ProtocolPhase.STANDARD_CHARGE,
                TerminationKind.VOLTAGE,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
                expected_event_names=(f"{config.cell.upper_cutoff_v} V",),
                allowed_physical_event_names=model_events,
            ),
            StageSpec(
                ProtocolPhase.STANDARD_CHARGE,
                TerminationKind.CURRENT,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
                expected_event_names=(f"{config.protocol.cv_cutoff_a} A",),
                allowed_physical_event_names=model_events,
            ),
        )

    def _stage_outcome_from_solution(
        self, solution: Any, spec: StageSpec, termination_value: float
    ) -> StageOutcome:
        terminal_time = float(solution.t[-1])
        state = _terminal_state_vector(solution)

        def scalar(name: str) -> float | None:
            try:
                return float(np.ravel(solution[name](terminal_time))[-1])
            except (KeyError, TypeError, ValueError):
                return None

        raw = str(solution.termination)
        return StageOutcome(
            termination_kind=map_termination(raw, spec),
            raw_termination=raw,
            termination_time_s=terminal_time,
            termination_value=termination_value,
            terminal_voltage_v=scalar("Terminal voltage [V]"),
            terminal_temperature_k=scalar("X-averaged cell temperature [K]"),
            terminal_discharge_capacity_ah=scalar("Discharge capacity [A.h]"),
            state_hash=sha256(state.tobytes()).hexdigest(),
        )

    def _extract_charge_stage_trace_from_solution(
        self,
        solution: Any,
        stage_name: str,
        resolved_variables: ResolvedChargeVariables,
        *,
        global_time_offset_s: float = 0.0,
    ) -> ChargeStageTrace:
        local_points = np.unique(np.asarray(solution.t, dtype=float))
        if len(local_points) < 2 or np.any(np.diff(local_points) <= 0):
            raise NumericalFailure(FailureContext(
                reason=FailureReason.INVALID_STATE,
                charge_stage=stage_name,
                message="charge stage time is not strictly increasing",
            ))
        values: dict[str, tuple[float, ...]] = {}
        try:
            for role, resolved_name in resolved_variables.roles:
                if resolved_name is None or role.key == "time_s":
                    continue
                values[role.key] = self._charge_series(
                    np.asarray(solution[resolved_name](local_points)), len(local_points), role.aggregation
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise NumericalFailure(FailureContext(
                reason=FailureReason.MISSING_MODEL_VARIABLE,
                charge_stage=stage_name,
                message=f"charge stage {stage_name} extraction failed: {exc}",
            )) from exc
        self._add_charge_inventory_conversions(values, stage_name)
        required = ("current_a", "terminal_voltage_v", "temperature_k", "negative_particle_lithium_mol")
        if any(
            key not in values
            or len(values[key]) != len(local_points)
            or not np.all(np.isfinite(values[key]))
            for key in required
        ):
            raise NumericalFailure(FailureContext(
                reason=FailureReason.INVALID_STATE,
                charge_stage=stage_name,
                message=f"charge stage {stage_name} contains invalid core samples",
            ))
        return ChargeStageTrace(
            stage_name,
            tuple(float(point) for point in local_points),
            values,
            global_time_offset_s=float(global_time_offset_s),
        )

    def _solve_standard_charge_attempt(
        self,
        config: RunConfig,
        snapshot: PyBaMMSnapshot,
        profile: SolverProfile,
        resolved_variables: ResolvedChargeVariables | None,
        on_stage_change: Callable[[str], None] | None,
    ) -> StandardChargeSequenceResult:
        import pybamm

        stage_wall: dict[str, float] = {}
        callback_state: dict[str, Any] = {
            "step_index": 0,
            "wall_start": None,
            "error": None,
            "infeasible_event": None,
        }

        class ChargeSequenceCallback(pybamm.callbacks.Callback):
            def on_step_start(inner_self, logs):
                step_index = int(logs["step number"][0]) - 1
                callback_state["step_index"] = step_index
                callback_state["wall_start"] = monotonic()
                if on_stage_change is not None and 0 <= step_index < len(STANDARD_CHARGE_STAGE_NAMES):
                    on_stage_change(STANDARD_CHARGE_STAGE_NAMES[step_index])

            def on_step_end(inner_self, logs):
                index = int(callback_state["step_index"])
                started = callback_state["wall_start"]
                if started is not None and 0 <= index < len(STANDARD_CHARGE_STAGE_NAMES):
                    stage_wall[STANDARD_CHARGE_STAGE_NAMES[index]] = monotonic() - float(started)

            def on_experiment_error(inner_self, logs):
                callback_state["error"] = logs.get("error")

            def on_experiment_infeasible_event(inner_self, logs):
                callback_state["infeasible_event"] = logs.get("termination")

        simulation = pybamm.Simulation(
            self.artifacts.model,
            parameter_values=self.artifacts.parameter_values,
            solver=build_solver(config, profile),
            experiment=build_standard_charge_experiment(config),
        )
        kwargs: dict[str, Any] = {
            "starting_solution": snapshot.solution,
            "showprogress": False,
            "callbacks": ChargeSequenceCallback(),
        }
        if snapshot.solution is None:
            kwargs["initial_soc"] = self.initial_soc
        try:
            candidate = simulation.solve(**kwargs)
        except Exception as exc:
            index = int(callback_state["step_index"])
            stage = STANDARD_CHARGE_STAGE_NAMES[min(max(index, 0), 3)]
            source = callback_state["error"] or exc
            raise SolverStepFailure.from_exception(source, index, stage) from exc

        cycles = getattr(candidate, "cycles", None)
        new_cycle = None if not cycles else cycles[-1]
        steps = () if new_cycle is None else tuple(getattr(new_cycle, "steps", ()))
        if len(steps) != 4:
            index = min(int(callback_state["step_index"]), 3)
            stage = STANDARD_CHARGE_STAGE_NAMES[index]
            if callback_state["infeasible_event"] is not None:
                raise PhysicalProtocolFailure(FailureContext(
                    reason=FailureReason.PHYSICAL_EVENT_BEFORE_TARGET,
                    phase=ProtocolPhase.STANDARD_CHARGE,
                    charge_stage=stage,
                    failed_step_index=index,
                    message=str(callback_state["infeasible_event"]),
                ))
            source = callback_state["error"]
            if source is not None:
                raise SolverStepFailure.from_exception(source, index, stage)
            raise SolverStepFailure(
                f"INCOMPLETE_CHARGE_SEQUENCE: expected 4 steps, got {len(steps)}",
                sundials_error_code=None,
                failed_step_index=index,
                charge_stage=stage,
                retryable=False,
            )

        specs = self._standard_charge_specs(config, self._model_event_names())
        values = (4.0, config.protocol.cv_cutoff_a, config.cell.upper_cutoff_v, config.protocol.cv_cutoff_a)
        local_outcomes = tuple(
            self._stage_outcome_from_solution(step, spec, value)
            for step, spec, value in zip(steps, specs, values, strict=True)
        )
        local_origin_s = float(candidate.t[0])
        outcomes = tuple(
            replace(
                outcome,
                termination_time_s=(
                    snapshot.time_s
                    + float(outcome.termination_time_s or 0.0)
                    - local_origin_s
                ),
            )
            if isinstance(outcome, StageOutcome)
            else outcome
            for outcome in local_outcomes
        )
        durations = {
            name: float(step.t[-1] - step.t[0])
            for name, step in zip(STANDARD_CHARGE_STAGE_NAMES, steps, strict=True)
        }
        traces = () if resolved_variables is None else tuple(
            self._extract_charge_stage_trace_from_solution(
                step,
                name,
                resolved_variables,
                global_time_offset_s=snapshot.time_s - local_origin_s,
            )
            for name, step in zip(STANDARD_CHARGE_STAGE_NAMES, steps, strict=True)
        )
        terminal_state = candidate.last_state
        terminal_vector = _terminal_state_vector(terminal_state)
        terminal_time = snapshot.time_s + float(terminal_state.t[-1]) - local_origin_s
        terminal_capacity = float(candidate["Discharge capacity [A.h]"](candidate.t[-1]))
        terminal_snapshot = PyBaMMSnapshot(
            candidate,
            terminal_time,
            terminal_time,
            terminal_capacity,
            sha256(terminal_vector.tobytes()).hexdigest(),
        )
        return StandardChargeSequenceResult(
            outcomes=outcomes,
            traces=traces,
            stage_durations_s=durations,
            stage_wall_clock_durations_s=stage_wall,
            terminal_snapshot=terminal_snapshot,
            attempt_count=1,
            solver_profile=profile.name,
            pre_charge_state_hash=snapshot.state_hash,
        )

    def run_standard_charge_sequence(
        self,
        config: RunConfig,
        *,
        resolved_variables: ResolvedChargeVariables | None = None,
        on_stage_change: Callable[[str, int, str], None] | None = None,
    ) -> StandardChargeSequenceResult:
        """Run the unchanged four-stage charge atomically with one bounded retry."""
        pre_charge = self.snapshot()
        failures: list[SolverAttemptFailure] = []
        profiles = (
            certified_charge_solver_profile(config),
            conservative_charge_solver_profile(config),
        )
        for attempt, profile in enumerate(profiles, start=1):
            if self.snapshot().state_hash != pre_charge.state_hash:
                raise NumericalFailure(FailureContext(
                    reason=FailureReason.INVALID_STATE,
                    phase=ProtocolPhase.STANDARD_CHARGE,
                    pre_charge_state_hash=pre_charge.state_hash,
                    solver_attempt=attempt,
                    solver_profile=profile.name,
                    message="standard charge retry state hash mismatch",
                ))
            try:
                result = self._solve_standard_charge_attempt(
                    config,
                    pre_charge,
                    profile,
                    resolved_variables,
                    None if on_stage_change is None else (
                        lambda name, a=attempt, p=profile.name: on_stage_change(name, a, p)
                    ),
                )
            except SolverStepFailure as exc:
                failures.append(SolverAttemptFailure(
                    attempt=attempt,
                    solver_profile=profile.name,
                    sundials_error_code=exc.sundials_error_code,
                    failed_step_index=exc.failed_step_index,
                    charge_stage=exc.charge_stage,
                    message=exc.raw_message,
                ))
                if attempt == 1 and exc.retryable:
                    continue
                exc.attempt_failures = tuple(failures)
                raise
            return replace(
                result,
                attempt_count=attempt,
                solver_profile=profile.name,
                initial_failure_code=(None if not failures else failures[0].sundials_error_code),
                attempt_failures=tuple(failures),
            )
        raise AssertionError("unreachable standard charge retry state")

    def commit_standard_charge_sequence(
        self, result: StandardChargeSequenceResult
    ) -> None:
        """Atomically commit a previously validated local-time charge candidate."""
        current = self.snapshot()
        if (
            result.pre_charge_state_hash is not None
            and current.state_hash != result.pre_charge_state_hash
        ):
            raise NumericalFailure(FailureContext(
                reason=FailureReason.INVALID_STATE,
                phase=ProtocolPhase.STANDARD_CHARGE,
                pre_charge_state_hash=result.pre_charge_state_hash,
                state_hash=current.state_hash,
                message="standard charge commit state hash mismatch",
            ))
        candidate = result.terminal_snapshot.solution
        local_duration_s = float(candidate.t[-1] - candidate.t[0])
        global_start_s = float(result.terminal_snapshot.time_s) - local_duration_s
        global_end_s = self._commit_local_candidate(candidate, global_start_s)
        if not np.isclose(global_end_s, result.terminal_snapshot.time_s, rtol=0, atol=1e-9):
            raise NumericalFailure(FailureContext(
                reason=FailureReason.INVALID_STATE,
                phase=ProtocolPhase.STANDARD_CHARGE,
                message="standard charge global time commit mismatch",
            ))
        final = result.outcomes[-1]
        self.last_termination = final.raw_termination
        self.last_termination_time_s = final.termination_time_s
        self.last_termination_value = final.termination_value
        self.last_outcome = final

    def _terminal_scalar(self, names: tuple[str, ...], time_s: float) -> float | None:
        try:
            return float(np.ravel(self._variable(names, time_s))[-1])
        except KeyError:
            return None

    def _model_event_names(self) -> tuple[str, ...]:
        return tuple(str(event.name) for event in self.artifacts.model.events)

    @staticmethod
    def _resolved_spec(provided: StageSpec | None, default: StageSpec) -> StageSpec:
        """Preserve protocol intent while filling backend-owned registered names."""
        if provided is None:
            return default
        return StageSpec(
            phase=provided.phase,
            expected_termination=provided.expected_termination,
            allowed_physical_terminations=(
                provided.allowed_physical_terminations or default.allowed_physical_terminations
            ),
            expected_event_names=provided.expected_event_names or default.expected_event_names,
            allowed_physical_event_names=(
                provided.allowed_physical_event_names or default.allowed_physical_event_names
            ),
        )

    def _set_termination_value(self, outcome: StageOutcome, value: float) -> StageOutcome:
        self.last_termination_value = value
        self.last_outcome = replace(outcome, termination_value=value)
        return self.last_outcome

    def cc_charge_to_voltage(
        self, current_a: float, voltage_v: float, *, spec: StageSpec | None = None
    ) -> StageOutcome:
        import pybamm

        stage = self._resolved_spec(spec, StageSpec(
            ProtocolPhase.STANDARD_CHARGE,
            TerminationKind.VOLTAGE,
            allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
            expected_event_names=(f"{voltage_v} V",),
            allowed_physical_event_names=self._model_event_names(),
        ))
        outcome = self._run(
            pybamm.step.current(-current_a, termination=f"{voltage_v} V"),
            stage,
            solver=getattr(self.artifacts, "charge_solver", self.artifacts.solver),
        )
        return self._set_termination_value(outcome, voltage_v)

    def cv_hold_to_current(
        self, voltage_v: float, cutoff_current_a: float, *, spec: StageSpec | None = None
    ) -> StageOutcome:
        import pybamm

        stage = self._resolved_spec(spec, StageSpec(
            ProtocolPhase.STANDARD_CHARGE,
            TerminationKind.CURRENT,
            allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
            expected_event_names=(f"{cutoff_current_a} A",),
            allowed_physical_event_names=self._model_event_names(),
        ))
        outcome = self._run(
            pybamm.step.voltage(voltage_v, termination=f"{cutoff_current_a} A"),
            stage,
            solver=getattr(self.artifacts, "charge_solver", self.artifacts.solver),
        )
        return self._set_termination_value(outcome, cutoff_current_a)

    def rest(self, duration_s: float, *, spec: StageSpec | None = None) -> StageOutcome:
        import pybamm

        stage = self._resolved_spec(
            spec,
            StageSpec(
                ProtocolPhase.POST_RPT_RECOVERY,
                TerminationKind.FINAL_TIME,
                allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
                allowed_physical_event_names=self._model_event_names(),
            ),
        )
        outcome = self._run(
            pybamm.step.rest(duration=duration_s),
            stage,
            solver=self.artifacts.solver,
        )
        return self._set_termination_value(outcome, duration_s)

    def discharge_to_voltage(
        self, current_a: float, voltage_v: float, *, spec: StageSpec | None = None
    ) -> StageOutcome:
        import pybamm

        stage = self._resolved_spec(spec, StageSpec(
            ProtocolPhase.RPT_CAPACITY_DISCHARGE,
            TerminationKind.VOLTAGE,
            allowed_physical_terminations=(TerminationKind.MODEL_PHYSICAL_EVENT,),
            expected_event_names=(f"{voltage_v} V",),
            allowed_physical_event_names=self._model_event_names(),
        ))
        outcome = self._run(
            pybamm.step.current(current_a, termination=f"{voltage_v} V"),
            stage,
            solver=self.artifacts.solver,
        )
        return self._set_termination_value(outcome, voltage_v)

    @staticmethod
    def _capacity_termination(q_start_ah: float, target_ah: float):
        import pybamm

        return pybamm.step.CustomTermination(
            "W10_CAPACITY_WINDOW",
            lambda variables: target_ah - (variables["Discharge capacity [A.h]"] - q_start_ah),
        )

    def discharge_to_capacity(
        self, current_a: float, q_start_ah: float, target_ah: float, voltage_v: float,
        *,
        spec: StageSpec | None = None,
    ) -> StageOutcome:
        import pybamm

        event = self._capacity_termination(q_start_ah, target_ah)
        stage = self._resolved_spec(spec, StageSpec(
            ProtocolPhase.STEP5_C4_DISCHARGE,
            TerminationKind.CAPACITY,
            allowed_physical_terminations=(TerminationKind.VOLTAGE, TerminationKind.MODEL_PHYSICAL_EVENT),
            expected_event_names=("W10_CAPACITY_WINDOW",),
            allowed_physical_event_names=(f"{voltage_v} V", *self._model_event_names()),
        ))
        outcome = self._run(
            pybamm.step.current(current_a, termination=[f"{voltage_v} V", event]),
            stage,
            solver=self.artifacts.solver,
        )
        return self._set_termination_value(outcome, target_ah)

    def drive_cycle_to_capacity(
        self, profile: CurrentProfile, q_start_ah: float, target_ah: float, voltage_v: float,
        *,
        spec: StageSpec | None = None,
    ) -> StageOutcome:
        import pybamm

        event = self._capacity_termination(q_start_ah, target_ah)
        drive_cycle = np.column_stack((profile.time_s, profile.current_a))
        stage = self._resolved_spec(spec, StageSpec(
            ProtocolPhase.STEP6_UDDS,
            TerminationKind.CAPACITY,
            allowed_physical_terminations=(TerminationKind.VOLTAGE, TerminationKind.MODEL_PHYSICAL_EVENT),
            expected_event_names=("W10_CAPACITY_WINDOW",),
            allowed_physical_event_names=(f"< {voltage_v} V", *self._model_event_names()),
        ))
        outcome = self._run(
            pybamm.step.current(drive_cycle, termination=[f"< {voltage_v} V", event]),
            stage,
            solver=self.artifacts.solver,
        )
        return self._set_termination_value(outcome, target_ah)

    def _variable(self, names: tuple[str, ...], time: float | np.ndarray) -> np.ndarray:
        if self.solution is None:
            raise KeyError(names[0])
        for name in names:
            try:
                return np.asarray(self.solution[name](time))
            except KeyError:
                continue
        raise KeyError(names[0])

    @staticmethod
    def _charge_series(raw: np.ndarray, point_count: int, aggregation: str) -> tuple[float, ...]:
        """Reduce a PyBaMM scalar or spatial field to one value per time point."""
        values = np.asarray(raw, dtype=float)
        if values.ndim == 0:
            return (float(values),) * point_count
        if values.size == point_count:
            return tuple(float(value) for value in values.reshape(-1))
        if values.shape[-1] == point_count:
            flat = values.reshape(-1, point_count)
            reduced = np.min(flat, axis=0) if aggregation == "spatiotemporal_min" else np.mean(flat, axis=0)
            return tuple(float(value) for value in reduced)
        if values.shape[0] == point_count:
            flat = values.reshape(point_count, -1)
            reduced = np.min(flat, axis=1) if aggregation == "spatiotemporal_min" else np.mean(flat, axis=1)
            return tuple(float(value) for value in reduced)
        raise ValueError("variable shape does not expose the requested time dimension")

    def extract_charge_stage_trace(
        self,
        stage_name: str,
        start_time_s: float,
        end_time_s: float,
        resolved_variables: ResolvedChargeVariables,
    ) -> ChargeStageTrace:
        """Read an inclusive charge-stage trace from the current continuous solution.

        This adapter never calls ``Simulation.solve``; it only evaluates
        already-solved PyBaMM variables at stage-local sample points.
        """
        if self.solution is None or not np.isfinite(start_time_s) or not np.isfinite(end_time_s) or end_time_s <= start_time_s:
            raise NumericalFailure(FailureContext(reason=FailureReason.INVALID_STATE, message="invalid charge stage extraction range"))
        segment = next(
            (
                item
                for item in reversed(self._committed_segments)
                if abs(item.global_start_s - start_time_s) <= 1e-8
                and abs(item.global_end_s - end_time_s) <= 1e-8
            ),
            None,
        )
        if segment is None:
            raise NumericalFailure(FailureContext(
                reason=FailureReason.INVALID_STATE,
                message=f"no committed local-time segment covers charge stage {stage_name}",
            ))
        local_origin_s = float(segment.local_solution.t[0])
        return self._extract_charge_stage_trace_from_solution(
            segment.local_solution,
            stage_name,
            resolved_variables,
            global_time_offset_s=segment.global_start_s - local_origin_s,
        )

    def _add_charge_inventory_conversions(
        self, values: dict[str, tuple[float, ...]], stage_name: str
    ) -> None:
        try:
            parameters = self.artifacts.parameter_values
            volume = (
                float(parameters["Negative electrode thickness [m]"])
                * float(parameters["Electrode width [m]"])
                * float(parameters["Electrode height [m]"])
            )
            conversion = volume * 96485.33212 / 3600.0
            values["dead_lithium_inventory_ah"] = tuple(
                value * conversion for value in values["dead_lithium_concentration_mol_m3"]
            )
            values["reversible_plating_inventory_ah"] = tuple(
                total - dead
                for total, dead in zip(
                    values["total_plating_inventory_ah"],
                    values["dead_lithium_inventory_ah"],
                    strict=True,
                )
            )
            values["cumulative_sei_loss_ah"] = tuple(
                standard + cracks
                for standard, cracks in zip(
                    values["negative_sei_inventory_ah"],
                    values["negative_sei_cracks_inventory_ah"],
                    strict=True,
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise NumericalFailure(FailureContext(
                reason=FailureReason.INVALID_STATE,
                charge_stage=stage_name,
                message=f"charge stage {stage_name} inventory conversion failed: {exc}",
            )) from exc

    def evaluate_charge_state(
        self, time_s: float, resolved_variables: ResolvedChargeVariables
    ) -> ChargeStateSnapshot:
        """Evaluate charge variables at one time without creating a new solution."""
        if self.solution is None:
            raise NumericalFailure(FailureContext(reason=FailureReason.INVALID_STATE, message="cannot evaluate an empty solution"))
        values: dict[str, float] = {}
        try:
            for role, resolved_name in resolved_variables.roles:
                if resolved_name is None or role.key == "time_s":
                    continue
                values[role.key] = self._charge_series(
                    np.asarray(self.solution[resolved_name](time_s)), 1, role.aggregation
                )[0]
        except (KeyError, TypeError, ValueError) as exc:
            raise NumericalFailure(FailureContext(
                reason=FailureReason.MISSING_MODEL_VARIABLE,
                message=f"charge state extraction failed: {exc}",
            )) from exc
        digest = sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()
        return ChargeStateSnapshot(time_s, values, digest)

    def summary_metrics(self, start_time_s: float | None = None) -> dict[str, float]:
        if self.solution is None:
            return {}
        final_t = float(self.solution.t[-1])
        scalar_names = {
            "terminal_voltage_v": ("Terminal voltage [V]",),
            "temperature_k": ("X-averaged cell temperature [K]",),
            "ambient_temperature_k": ("Ambient temperature [K]",),
            "lli_pct": ("Loss of lithium inventory [%]",),
            "negative_lam_pct": ("Loss of active material in negative electrode [%]",),
            "positive_lam_pct": ("Loss of active material in positive electrode [%]",),
            "negative_sei_thickness_m": ("X-averaged negative SEI thickness [m]",),
            "negative_sei_on_cracks_thickness_m": (
                "X-averaged negative SEI on cracks thickness [m]",
            ),
            "negative_porosity": ("X-averaged negative electrode porosity",),
            "positive_porosity": ("X-averaged positive electrode porosity",),
            "negative_active_material_fraction": (
                "X-averaged negative electrode active material volume fraction",
            ),
            "positive_active_material_fraction": (
                "X-averaged positive electrode active material volume fraction",
            ),
        }
        output: dict[str, float] = {}
        for key, names in scalar_names.items():
            try:
                output[key] = float(np.ravel(self._variable(names, final_t))[-1])
            except KeyError:
                continue
        try:
            normal_sei = float(np.ravel(self._variable(
                ("Loss of capacity to negative SEI [A.h]",), final_t
            ))[-1])
            sei_on_cracks = float(np.ravel(self._variable(
                ("Loss of capacity to negative SEI on cracks [A.h]",), final_t
            ))[-1])
            output.update({
                "normal_sei_loss_ah": normal_sei,
                "sei_on_cracks_loss_ah": sei_on_cracks,
                "total_sei_loss_ah": normal_sei + sei_on_cracks,
                "sei_loss_ah": normal_sei + sei_on_cracks,
            })
        except KeyError:
            pass
        try:
            total_plated = float(np.ravel(self._variable(
                ("Loss of capacity to negative lithium plating [A.h]",), final_t
            ))[-1])
            output["total_plated_lithium_ah"] = total_plated
            output["plating_loss_ah"] = total_plated
        except KeyError:
            pass
        try:
            negative_dead = float(
                np.ravel(
                    self._variable(
                        ("Volume-averaged negative dead lithium concentration [mol.m-3]",),
                        final_t,
                    )
                )[-1]
            )
            parameters = self.artifacts.parameter_values
            negative_volume = (
                float(parameters["Electrode height [m]"])
                * float(parameters["Electrode width [m]"])
                * float(parameters["Negative electrode thickness [m]"])
            )
            dead_lithium = 96485.33212 * negative_dead * negative_volume / 3600.0
            output["dead_lithium_ah"] = dead_lithium
            output["dead_lithium_loss_ah"] = dead_lithium
            if "total_plated_lithium_ah" in output:
                output["reversible_plated_lithium_ah"] = (
                    output["total_plated_lithium_ah"] - dead_lithium
                )
        except KeyError:
            pass
        trace = self.timeseries_since(start_time_s or 0.0)
        if trace:
            current = trace.get("current_a")
            time = trace["time_s"]
            temperature = trace.get("temperature_k")
            if current is not None and len(time) > 1:
                output["charge_ah"] = float(np.trapezoid(np.maximum(-current, 0), time) / 3600)
                output["discharge_ah"] = float(np.trapezoid(np.maximum(current, 0), time) / 3600)
                output["net_ah"] = output["discharge_ah"] - output["charge_ah"]
            if temperature is not None:
                maximum_index = int(np.argmax(temperature))
                output["temperature_min_k"] = float(np.min(temperature))
                output["temperature_max_k"] = float(temperature[maximum_index])
                ambient = trace.get("ambient_temperature_k")
                if ambient is not None:
                    output["ambient_temperature_k"] = float(ambient[maximum_index])
                if "ambient_temperature_k" in output:
                    output["temperature_rise_max_k"] = (
                        output["temperature_max_k"] - output["ambient_temperature_k"]
                    )
        return output

    def timeseries_since(self, start_time_s: float = 0.0) -> dict[str, np.ndarray]:
        if self.solution is None:
            return {}
        if not hasattr(self, "_committed_segments"):
            all_time = np.asarray(self.solution.t, dtype=float)
            mask = all_time >= start_time_s - 1e-9
            time = all_time[mask]
            values: dict[str, np.ndarray] = {"time_s": time}
            for key, names in {
                "current_a": ("Current [A]",),
                "terminal_voltage_v": ("Terminal voltage [V]",),
                "temperature_k": ("X-averaged cell temperature [K]",),
                "ambient_temperature_k": ("Ambient temperature [K]",),
                "discharge_capacity_ah": ("Discharge capacity [A.h]",),
            }.items():
                try:
                    values[key] = np.asarray(self._variable(names, time)).reshape(-1)
                except KeyError:
                    pass
            return values if len(values) > 1 and all(len(value) == len(time) for value in values.values()) else {}
        if not self._committed_segments:
            return {}
        variable_names = {
            "current_a": ("Current [A]",),
            "terminal_voltage_v": ("Terminal voltage [V]",),
            "temperature_k": ("X-averaged cell temperature [K]",),
            "ambient_temperature_k": ("Ambient temperature [K]",),
            "discharge_capacity_ah": ("Discharge capacity [A.h]",),
        }
        accumulated: dict[str, list[np.ndarray]] = {"time_s": []}
        accumulated.update({key: [] for key in variable_names})
        last_global_s: float | None = None
        for segment in self._committed_segments:
            local_time = np.asarray(segment.local_solution.t, dtype=float)
            if local_time.size == 0:
                continue
            global_time = segment.global_start_s + (local_time - float(local_time[0]))
            mask = global_time >= start_time_s - 1e-9
            if last_global_s is not None:
                mask &= global_time > last_global_s + 1e-9
            if not np.any(mask):
                continue
            selected_local = local_time[mask]
            selected_global = global_time[mask]
            segment_values: dict[str, np.ndarray] = {}
            complete = True
            for key, names in variable_names.items():
                for name in names:
                    try:
                        raw = np.asarray(
                            segment.local_solution[name](selected_local), dtype=float
                        ).reshape(-1)
                    except KeyError:
                        continue
                    if raw.size != selected_global.size:
                        complete = False
                    else:
                        segment_values[key] = raw
                    break
                if key not in segment_values:
                    complete = False
            if not complete:
                continue
            accumulated["time_s"].append(selected_global)
            for key, raw in segment_values.items():
                accumulated[key].append(raw)
            last_global_s = float(selected_global[-1])
        if not accumulated["time_s"]:
            return {}
        result = {
            key: np.concatenate(parts)
            for key, parts in accumulated.items()
            if parts
        }
        time = result["time_s"]
        return result if len(result) > 1 and all(len(value) == len(time) for value in result.values()) else {}

    def timeseries(self) -> dict[str, np.ndarray]:
        return self.timeseries_since(0.0)
