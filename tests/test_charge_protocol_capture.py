from __future__ import annotations

import numpy as np

from pybamm_w10.config import RunConfig
from pybamm_w10.protocol import ProtocolStateMachine
from pybamm_w10.types import ChargeStageTrace, StageOutcome
from pybamm_w10.udds import CurrentProfile
from conftest import fake_standard_charge_sequence


class CaptureBackend:
    run_standard_charge_sequence = fake_standard_charge_sequence

    def __init__(self) -> None:
        self.time = 0.0
        self.q = 0.0
        self.events: list[str] = []
        self.extracted: list[str] = []

    def current_time_s(self): return self.time
    def discharge_capacity_ah(self): return self.q
    def _outcome(self, spec): return StageOutcome(spec.expected_termination, spec.expected_termination.value, self.time, None, 3.5, 296.15, self.q, "state")
    def cc_charge_to_voltage(self, *_args, spec): self.events.append("cc"); self.time += 100; return self._outcome(spec)
    def cv_hold_to_current(self, *_args, spec): self.events.append("cv"); self.time += 100; return self._outcome(spec)
    def rest(self, *_args, spec): self.events.append("rest"); self.time += 100; return self._outcome(spec)
    def discharge_to_capacity(self, _current, start, target, _voltage, *, spec): self.events.append("step5"); self.q = start + target; self.time += 100; return self._outcome(spec)
    def drive_cycle_to_capacity(self, _profile, start, target, _voltage, *, spec): self.events.append("step6"); self.q = start + target; self.time += 100; return self._outcome(spec)
    def summary_metrics(self, *_args): return {}

    def extract_charge_stage_trace(self, name, start, end, _resolved):
        self.extracted.append(name)
        faraday = 96485.33212
        start_lithium = (len(self.extracted) - 1) * 0.8 * 3600 / faraday
        end_lithium = len(self.extracted) * 0.8 * 3600 / faraday
        return ChargeStageTrace(name, (start, end), {
            "current_a": (-14.55, -14.55), "terminal_voltage_v": (3.5, 4.0),
            "temperature_k": (296.15, 296.2), "negative_particle_lithium_mol": (start_lithium, end_lithium),
            "total_plating_inventory_ah": (0.0, 0.0), "dead_lithium_inventory_ah": (0.0, 0.0),
            "reversible_plating_inventory_ah": (0.0, 0.0), "cumulative_sei_loss_ah": (0.0, 0.0),
            "negative_electrode_surface_potential_difference_v": (0.1, 0.05),
        })


def test_protocol_captures_exactly_four_charge_stages_before_rest() -> None:
    backend = CaptureBackend()
    profile = CurrentProfile(np.array([0.0, 3600.0]), np.array([1.0, 1.0]))
    machine = ProtocolStateMachine(RunConfig(), profile, resolved_charge_variables=object(), q_ref_initial_ah=4.0)

    result = machine.run_standard_cycle(backend, 1, 4.0, 0)

    assert backend.extracted == ["3c_cc", "4v_cv", "c4_cc", "4p2v_cv"]
    assert backend.events.index("rest") == len(backend.extracted)
    assert result.charge_analysis is not None
    assert result.complete_soc_bin_count == 4
    assert result.metrics["negative_electrode_min_potential_v"] == 0.05
