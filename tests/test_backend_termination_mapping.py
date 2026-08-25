from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from pybamm_w10.backend import _terminal_state_vector, map_termination
from pybamm_w10.types import ProtocolPhase, StageSpec, TerminationKind


def test_stage_limited_termination_mapping_uses_registered_full_names() -> None:
    capacity = StageSpec(
        ProtocolPhase.STEP6_UDDS,
        TerminationKind.CAPACITY,
        allowed_physical_terminations=(TerminationKind.VOLTAGE, TerminationKind.MODEL_PHYSICAL_EVENT),
        expected_event_names=("W10_CAPACITY_WINDOW",),
        allowed_physical_event_names=("2.5 V", "Negative electrode porosity"),
    )

    assert map_termination("event: W10_CAPACITY_WINDOW", capacity) is TerminationKind.CAPACITY
    assert map_termination("event: W10_CAPACITY_WINDOW [experiment]", capacity) is TerminationKind.CAPACITY
    assert map_termination("event: 2.5 V", capacity) is TerminationKind.VOLTAGE
    assert map_termination("event: Negative electrode porosity", capacity) is TerminationKind.MODEL_PHYSICAL_EVENT
    assert map_termination("final time", capacity) is TerminationKind.FINAL_TIME
    assert map_termination("event: unrelated boundary", capacity) is TerminationKind.UNKNOWN


def test_mapping_does_not_use_broad_voltage_or_current_substrings() -> None:
    current = StageSpec(
        ProtocolPhase.STANDARD_CHARGE,
        TerminationKind.CURRENT,
        expected_event_names=("0.05 A",),
    )

    assert map_termination("event: arbitrary current-like text", current) is TerminationKind.UNKNOWN
    assert map_termination("event: 0.05 A", current) is TerminationKind.CURRENT
    assert map_termination("event: Current < 0.05 [A] [experiment]", current) is TerminationKind.CURRENT
    assert map_termination("event: abs(Current [A]) < 0.05 [A] [experiment]", current) is TerminationKind.CURRENT
    voltage = StageSpec(
        ProtocolPhase.STANDARD_CHARGE,
        TerminationKind.VOLTAGE,
        expected_event_names=("4.0 V",),
    )
    assert map_termination("event: Voltage > 4.0 [V] [experiment]", voltage) is TerminationKind.VOLTAGE
    assert map_termination("event: Voltage > 3.9 [V] [experiment]", voltage) is TerminationKind.UNKNOWN
    with pytest.raises(ValueError):
        map_termination(None, current)


def test_terminal_state_hash_input_does_not_concatenate_incompatible_history() -> None:
    class CompositeSolution:
        last_state = SimpleNamespace(y=np.array([[1.0], [2.0]]))

        @property
        def y(self):
            raise RuntimeError("historical states have incompatible dimensions")

    assert _terminal_state_vector(CompositeSolution()).tolist() == [1.0, 2.0]
