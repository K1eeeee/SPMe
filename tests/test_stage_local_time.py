from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from pybamm_w10.backend import PyBaMMBackend, PyBaMMSnapshot
from pybamm_w10.types import ProtocolPhase, StageOutcome, StageSpec, TerminationKind


def test_restore_uses_snapshot_elapsed_time_not_solution_time() -> None:
    backend = PyBaMMBackend(SimpleNamespace())
    local_solution = SimpleNamespace(t=np.asarray([0.0]))
    snapshot = PyBaMMSnapshot(
        solution=local_solution,
        time_s=683_778.0,
        calendar_time_s=683_778.0,
        discharge_capacity_ah=1.25,
        state_hash="state",
    )

    backend.restore(snapshot)

    assert backend.current_time_s() == 683_778.0
    assert backend.calendar_time_s() == 683_778.0


def test_compact_state_preserves_external_elapsed_time(monkeypatch) -> None:
    backend = PyBaMMBackend(SimpleNamespace())
    terminal = SimpleNamespace(t=np.asarray([9.0]))
    backend.solution = SimpleNamespace(last_state=terminal, t=np.asarray([0.0, 9.0]))
    backend._elapsed_time_s = 123.0
    backend._committed_segments = [object()]

    monkeypatch.setattr(
        "pybamm_w10.backend._rebase_terminal_solution", lambda solution: terminal
    )
    backend.compact_state()

    assert backend.solution is terminal
    assert backend.current_time_s() == 123.0
    assert backend._committed_segments == []


def test_single_stage_solves_at_local_zero_and_commits_global_duration(monkeypatch) -> None:
    import pybamm

    local_start = SimpleNamespace(t=np.asarray([0.0]))
    candidate = SimpleNamespace(t=np.asarray([0.0, 5.0]))
    observed = {}

    class FakeSimulation:
        def __init__(self, *_args, **_kwargs):
            pass

        def solve(self, **kwargs):
            observed["starting_solution"] = kwargs["starting_solution"]
            return candidate

    backend = PyBaMMBackend(SimpleNamespace(model=object(), parameter_values=object(), solver=object()))
    backend.solution = SimpleNamespace(t=np.asarray([9.0]))
    backend._elapsed_time_s = 100.0
    monkeypatch.setattr("pybamm_w10.backend._rebase_terminal_solution", lambda _solution: local_start)
    monkeypatch.setattr("pybamm_w10.backend._terminal_state_vector", lambda _solution: np.asarray([1.0]))
    monkeypatch.setattr(backend, "discharge_capacity_ah", lambda: 1.0)
    monkeypatch.setattr(pybamm, "Simulation", FakeSimulation)
    monkeypatch.setattr(
        backend,
        "_stage_outcome_from_solution",
        lambda *_args: StageOutcome(
            TerminationKind.FINAL_TIME, "final time", 5.0, 0.0, 4.0, 298.0, 1.0, "hash"
        ),
    )

    outcome = backend._run(
        pybamm.step.rest(5),
        StageSpec(ProtocolPhase.POST_RPT_RECOVERY, TerminationKind.FINAL_TIME),
    )

    assert observed["starting_solution"] is local_start
    assert outcome.termination_time_s == 105.0
    assert backend.current_time_s() == 105.0
    assert backend._committed_segments[0].global_start_s == 100.0


def test_rpt_charge_uses_certified_profile_while_rest_uses_general_profile(monkeypatch) -> None:
    import pybamm

    general = object()
    charge = object()
    backend = PyBaMMBackend(
        SimpleNamespace(model=SimpleNamespace(events=()), parameter_values=object(), solver=general, charge_solver=charge)
    )
    observed = []

    def run(_step, _spec, *, solver=None):
        observed.append(solver)
        return StageOutcome(TerminationKind.FINAL_TIME, "final time", 1.0, 0.0, 4.0, 298.0, 1.0, "hash")

    monkeypatch.setattr(backend, "_run", run)
    monkeypatch.setattr(backend, "_model_event_names", lambda: ())

    backend.cc_charge_to_voltage(1.0, 4.0)
    backend.cv_hold_to_current(4.0, 0.05)
    backend.rest(30.0)

    assert observed == [charge, charge, general]
