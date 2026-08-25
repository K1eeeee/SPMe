from __future__ import annotations

from pybamm_w10.backend import construct_initial_state_record
from pybamm_w10.config import RunConfig
from pybamm_w10.model import aging_options, build_spme


def test_required_aging_options_are_enabled() -> None:
    options = aging_options()
    assert options["thermal"] == "lumped"
    assert options["lithium plating"] == "partially reversible"
    assert options["x-average side reactions"] == "false"


def test_spme_constructs_without_solving() -> None:
    artifacts = build_spme(RunConfig())
    assert artifacts.model.name == "Single Particle Model with electrolyte"
    assert artifacts.parameter_values["Nominal cell capacity [A.h]"] == 4.85


def test_canonical_initial_state_is_explicit_20pct_and_fingerprinted() -> None:
    config = RunConfig()
    record = construct_initial_state_record(build_spme(config), config)
    assert record.initial_soc == 0.20
    assert record.method == "pybamm.Simulation.build(initial_soc=...)"
    assert record.state_size > 100
    assert len(record.state_sha256) == 64
    assert 0 < record.negative_stoichiometry < 1
    assert 0 < record.positive_stoichiometry < 1
    second = construct_initial_state_record(build_spme(config), config)
    assert second.fingerprint == record.fingerprint
    assert second.state_sha256 == record.state_sha256
