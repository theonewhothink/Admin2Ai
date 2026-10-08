"""Bounded, deterministic subset-sum search (§20: 1 payment -> many invoices,
many payments -> 1 invoice).

Values are exact integers (money scaled by a power of ten) and may be negative
(credit notes inside a combined payment). Items are explored in the order given,
so the caller controls determinism. The search stops at ``node_limit`` and says
so: a capped search can never prove that a solution is unique.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["SubsetSearch", "find_subsets"]


@dataclass(frozen=True)
class SubsetSearch:
    """Solutions (ascending index tuples, discovery order) and how the search ended."""

    solutions: tuple[tuple[int, ...], ...]
    nodes: int
    capped: bool  # node limit reached before the search space was exhausted
    truncated: bool  # stopped after ``max_solutions`` were found

    @property
    def complete(self) -> bool:
        """True when every subset within the size bounds was considered."""
        return not self.capped and not self.truncated


def find_subsets(
    values: Sequence[int],
    target: int,
    *,
    min_size: int = 2,
    max_size: int = 6,
    max_solutions: int = 8,
    node_limit: int = 50_000,
) -> SubsetSearch:
    """All index subsets (size ``min_size``..``max_size``) whose values sum to ``target``.

    Depth-first over the given order with a reachability bound: the remaining
    target must lie between the sum of the remaining negatives and the sum of the
    remaining positives, otherwise the branch is pruned.
    """
    if min_size < 1 or max_size < min_size:
        raise ValueError("need 1 <= min_size <= max_size")
    n = len(values)
    low = [0] * (n + 1)
    high = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        low[i] = low[i + 1] + min(values[i], 0)
        high[i] = high[i + 1] + max(values[i], 0)
    state = _State(node_limit=node_limit, max_solutions=max_solutions)
    _dfs(values, target, 0, [], min_size, max_size, low, high, state)
    return SubsetSearch(
        solutions=tuple(state.solutions),
        nodes=state.nodes,
        capped=state.capped,
        truncated=state.truncated,
    )


@dataclass
class _State:
    node_limit: int
    max_solutions: int
    nodes: int = 0
    capped: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        self.solutions: list[tuple[int, ...]] = []

    @property
    def stopped(self) -> bool:
        return self.capped or self.truncated


def _dfs(
    values: Sequence[int],
    remaining: int,
    start: int,
    chosen: list[int],
    min_size: int,
    max_size: int,
    low: list[int],
    high: list[int],
    state: _State,
) -> None:
    if remaining == 0 and len(chosen) >= min_size:
        state.solutions.append(tuple(chosen))
        if len(state.solutions) >= state.max_solutions:
            state.truncated = True
            return
    if len(chosen) == max_size:
        return
    for i in range(start, len(values)):
        if state.stopped:
            return
        if not low[i] <= remaining <= high[i]:
            return  # nothing from i onwards can reach the target
        state.nodes += 1
        if state.nodes > state.node_limit:
            state.capped = True
            return
        chosen.append(i)
        _dfs(
            values,
            remaining - values[i],
            i + 1,
            chosen,
            min_size,
            max_size,
            low,
            high,
            state,
        )
        chosen.pop()
