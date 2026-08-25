from __future__ import annotations

from pybamm_w10.config import RunConfig
from pybamm_w10.model import (
    build_spme,
    effective_parameters_audit,
    effective_parameters_fingerprint,
)


def test_effective_parameter_audit_records_overrides_and_unset_rpt_fields() -> None:
    config = RunConfig()
    audit = effective_parameters_audit(build_spme(config), config)
    assert audit["model"] == "SPMe"

    nominal = audit["parameters"]["nominal_cell_capacity_ah"]
    assert nominal["original"] != config.cell.nominal_capacity_ah
    assert nominal["effective"] == config.cell.nominal_capacity_ah
    assert nominal["source"] == "m50t_experimental_override"
    assert audit["rpt"]["cycle_0_capacity_ah"] is None
    assert audit["calibration"]["capacity_scale_factor"]["value"] is None
    assert audit["calibration"]["sei_scale"]["value"] == 1.0
    assert set(audit["theoretical_capacity_window_ah"]) == {"negative", "positive"}
    assert set(audit["stoichiometry_endpoints"]) == {"negative", "positive"}


def test_effective_parameter_audit_fingerprint_is_stable_and_changes_after_rpt() -> None:
    config = RunConfig()
    artifacts = build_spme(config)
    before = effective_parameters_audit(artifacts, config)
    repeated = effective_parameters_audit(artifacts, config)
    after = effective_parameters_audit(artifacts, config, cycle_0_capacity_ah=4.865)

    assert effective_parameters_fingerprint(before) == effective_parameters_fingerprint(repeated)
    assert effective_parameters_fingerprint(before) != effective_parameters_fingerprint(after)
    assert after["rpt"]["cycle_0_capacity_ah"] == 4.865
