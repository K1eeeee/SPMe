from __future__ import annotations

import pytest

from pybamm_w10.calibration.surrogate import (
    SurrogateExecutionDisabled,
    SurrogateSearchBudget,
    require_surrogate_execution_authorization,
)


def test_surrogate_budget_is_fixed_and_execution_is_disabled() -> None:
    budget = SurrogateSearchBudget()
    assert (budget.candidates_total, budget.survivors_cycle_25, budget.survivors_cycle_75, budget.survivors_cycle_122, budget.survivors_cycle_225) == (32, 16, 8, 4, 2)
    assert budget.adaptive_budget_expansion is False
    with pytest.raises(SurrogateExecutionDisabled, match="not authorised"):
        require_surrogate_execution_authorization()
