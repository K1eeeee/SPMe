from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from pybamm_w10.backend import PyBaMMBackend, PyBaMMSnapshot
from pybamm_w10.config import RunConfig
from pybamm_w10.types import SolverStepFailure


def test_successful_sequence_commits_full_candidate_history(monkeypatch) -> None:
    import pybamm
    import pybamm_w10.backend as backend_module

    steps = tuple(
        SimpleNamespace(t=np.asarray([float(index), float(index + 1)]), termination="event")
        for index in range(4)
    )

    class FakeCandidate:
        cycles = [SimpleNamespace(steps=steps)]
        t = np.asarray([0.0, 4.0])
        last_state = SimpleNamespace(t=np.asarray([4.0]))

        def __getitem__(self, _name):
            return lambda _time: 0.0

    candidate = FakeCandidate()

    class FakeSimulation:
        def __init__(self, *_args, **_kwargs):
            pass

        def solve(self, **_kwargs):
            return candidate

    monkeypatch.setattr(pybamm, "Simulation", FakeSimulation)
    monkeypatch.setattr(backend_module, "build_solver", lambda *_args: object())
    monkeypatch.setattr(backend_module, "_terminal_state_vector", lambda _solution: np.asarray([1.0]))
    monkeypatch.setattr(
        PyBaMMBackend,
        "_stage_outcome_from_solution",
        lambda _self, _solution, spec, value: SimpleNamespace(
            termination_kind=spec.expected_termination,
            raw_termination="event",
            termination_time_s=float(value),
        ),
    )
    backend = PyBaMMBackend(
        SimpleNamespace(model=SimpleNamespace(events=()), parameter_values=object())
    )

    result = backend._solve_standard_charge_attempt(
        RunConfig(), backend.snapshot(), SimpleNamespace(name="default"), None, None
    )

    assert result.terminal_snapshot.solution is candidate


def test_partial_cycle_with_callback_error_is_never_committed(monkeypatch) -> None:
    import pybamm
    import pybamm_w10.backend as backend_module

    class FakeSimulation:
        def __init__(self, *_args, **_kwargs):
            pass

        def solve(self, **kwargs):
            callback = kwargs["callbacks"]
            callback.on_step_start({"step number": (2, 4)})
            callback.on_experiment_error({"error": RuntimeError("IDA_ERR_FAIL: injected")})
            return SimpleNamespace(cycles=[SimpleNamespace(steps=(object(),))])

    monkeypatch.setattr(pybamm, "Simulation", FakeSimulation)
    monkeypatch.setattr(backend_module, "build_solver", lambda *_args: object())
    backend = PyBaMMBackend(SimpleNamespace(model=object(), parameter_values=object()))
    before = backend.snapshot()

    with pytest.raises(SolverStepFailure) as caught:
        backend._solve_standard_charge_attempt(
            RunConfig(), before, SimpleNamespace(name="default"), None, None
        )

    assert caught.value.sundials_error_code == "IDA_ERR_FAIL"
    assert caught.value.failed_step_index == 1
    assert backend.snapshot().state_hash == before.state_hash


def test_partial_cycle_without_solver_error_is_explicitly_rejected(monkeypatch) -> None:
    import pybamm
    import pybamm_w10.backend as backend_module

    class FakeSimulation:
        def __init__(self, *_args, **_kwargs):
            pass

        def solve(self, **_kwargs):
            return SimpleNamespace(cycles=[SimpleNamespace(steps=())])

    monkeypatch.setattr(pybamm, "Simulation", FakeSimulation)
    monkeypatch.setattr(backend_module, "build_solver", lambda *_args: object())
    backend = PyBaMMBackend(SimpleNamespace(model=object(), parameter_values=object()))

    with pytest.raises(SolverStepFailure, match="INCOMPLETE_CHARGE_SEQUENCE"):
        backend._solve_standard_charge_attempt(
            RunConfig(), PyBaMMSnapshot(None, 0, 0, 0, "unbuilt-initial"),
            SimpleNamespace(name="default"), None, None,
        )
