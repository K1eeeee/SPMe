from __future__ import annotations

from pathlib import Path
import shutil
from uuid import uuid4

import pytest


def fake_standard_charge_sequence(
    self, config, *, resolved_variables=None, on_stage_change=None
):
    """Test-only adapter for legacy fake backends after the production API became atomic."""
    from types import SimpleNamespace
    from pybamm_w10.types import StandardChargeSequenceResult, StageSpec, ProtocolPhase, TerminationKind

    calls = (
        ("3c_cc", StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.VOLTAGE),
         lambda spec: self.cc_charge_to_voltage(config.protocol.charge_3c_a, 4.0, spec=spec)),
        ("4v_cv", StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.CURRENT),
         lambda spec: self.cv_hold_to_current(4.0, config.protocol.cv_cutoff_a, spec=spec)),
        ("c4_cc", StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.VOLTAGE),
         lambda spec: self.cc_charge_to_voltage(config.protocol.discharge_c4_a, config.cell.upper_cutoff_v, spec=spec)),
        ("4p2v_cv", StageSpec(ProtocolPhase.STANDARD_CHARGE, TerminationKind.CURRENT),
         lambda spec: self.cv_hold_to_current(config.cell.upper_cutoff_v, config.protocol.cv_cutoff_a, spec=spec)),
    )
    outcomes = []
    traces = []
    durations = {}
    for name, spec, call in calls:
        if on_stage_change is not None:
            on_stage_change(name, 1, "default")
        before = self.current_time_s()
        outcome = call(spec)
        outcomes.append(outcome)
        durations[name] = self.current_time_s() - before
        if resolved_variables is not None and hasattr(self, "extract_charge_stage_trace"):
            traces.append(self.extract_charge_stage_trace(
                name, before, self.current_time_s(), resolved_variables
            ))
    return StandardChargeSequenceResult(
        outcomes=tuple(outcomes), traces=tuple(traces), stage_durations_s=durations,
        stage_wall_clock_durations_s={name: 0.0 for name, _, _ in calls},
        terminal_snapshot=SimpleNamespace(state_hash="fake"), attempt_count=1,
        solver_profile="default",
    )


@pytest.fixture
def workspace_tmp():
    path = Path.cwd() / "tmp" / "pybamm_w10_tests" / uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
