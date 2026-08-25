"""Fixed future surrogate-search budget; execution remains deliberately disabled."""

from __future__ import annotations

from dataclasses import dataclass


class SurrogateExecutionDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class SurrogateSearchBudget:
    search_seed: int = 20260818
    candidates_total: int = 32
    survivors_cycle_25: int = 16
    survivors_cycle_75: int = 8
    survivors_cycle_122: int = 4
    survivors_cycle_225: int = 2
    full_dfn_candidates: int = 2
    full_dfn_validation_nodes: tuple[int, ...] = (25, 75, 122, 225)
    adaptive_budget_expansion: bool = False


def require_surrogate_execution_authorization() -> None:
    raise SurrogateExecutionDisabled(
        "surrogate execution is not authorised in stage I; no aging candidate was started"
    )
