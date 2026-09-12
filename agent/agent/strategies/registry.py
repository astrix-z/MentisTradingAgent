"""Strategy registry (kept separate to avoid import cycles).

Concrete strategies are registered with the :func:`strategy` decorator and
resolved by name with :func:`build_strategy`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from agent.strategies.base import Strategy

_StrategyT = TypeVar("_StrategyT", bound=Strategy)

#: Registry: strategy name -> strategy class.
STRATEGIES: dict[str, type[Strategy]] = {}


def strategy(name: str) -> Callable[[type[_StrategyT]], type[_StrategyT]]:
    """Register a :class:`Strategy` subclass under ``name``."""

    def register(cls: type[_StrategyT]) -> type[_StrategyT]:
        cls.name = name
        STRATEGIES[name] = cls
        return cls

    return register


def build_strategy(name: str, params: dict[str, Any]) -> Strategy:
    """Instantiate the registered strategy ``name`` with ``params``."""
    try:
        cls = STRATEGIES[name]
    except KeyError:
        known = ", ".join(sorted(STRATEGIES)) or "<none>"
        raise ValueError(f"unknown strategy {name!r}; known: {known}") from None
    return cls(params)


__all__ = ["STRATEGIES", "build_strategy", "strategy"]
