from __future__ import annotations

import pytest

from pybamm_w10.charge_variables import negative_electrode_volume_m3, plating_inventories_ah


def test_plating_inventory_primary_value_is_total_minus_dead_and_crosscheck_is_geometric() -> None:
    faraday = 96485.33212
    volume = negative_electrode_volume_m3(2.0, 3.0, 4.0)
    dead_concentration = 3600.0 / (volume * faraday)
    reversible_concentration = 2.0 * 3600.0 / (volume * faraday)

    values = plating_inventories_ah(3.0, dead_concentration, reversible_concentration, volume, faraday)

    assert values.dead_lithium_inventory_ah == pytest.approx(1.0)
    assert values.reversible_plating_inventory_ah == pytest.approx(2.0)
    assert values.reversible_plating_inventory_crosscheck_ah == pytest.approx(2.0)
