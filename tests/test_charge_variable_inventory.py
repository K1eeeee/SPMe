from __future__ import annotations

import json

import pytest

from pybamm_w10.charge_variables import (
    CHARGE_VARIABLE_ROLES,
    preflight_charge_variables,
    write_charge_efficiency_variable_inventory,
)
from pybamm_w10.types import NumericalFailure


class FakeModel:
    name = "SPMe"

    def __init__(self, variables: dict[str, object]) -> None:
        self.variables = variables


def _all_variables() -> dict[str, object]:
    return {
        candidate: object()
        for role in CHARGE_VARIABLE_ROLES
        for candidate in role.candidate_names[:1]
    }


def test_preflight_resolves_required_roles_and_writes_a_hashed_inventory(workspace_tmp) -> None:
    resolved = preflight_charge_variables(FakeModel(_all_variables()), model_options={"model": "SPMe"})

    assert resolved.core_preflight_passed is True
    assert resolved.mechanism_preflight_passed is True
    assert resolved.resolved_name("negative_particle_lithium_mol") == "Total lithium in negative electrode [mol]"
    path = write_charge_efficiency_variable_inventory(workspace_tmp / "inventory.json", resolved)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["inventory_schema_version"] == 1
    assert payload["inventory_sha256"]
    assert payload["roles"]["negative_particle_lithium_mol"]["available"] is True


def test_missing_required_role_stops_preflight_before_any_cycle() -> None:
    variables = _all_variables()
    variables.pop("Total lithium in negative electrode [mol]")

    with pytest.raises(NumericalFailure, match="MISSING_MODEL_VARIABLE"):
        preflight_charge_variables(FakeModel(variables), model_options={})
