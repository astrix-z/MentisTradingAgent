"""Strategies package.

Public surface: the :class:`Strategy` contract plus the registry. Importing this
package imports every concrete strategy so their ``@strategy`` decorators run.
"""

from __future__ import annotations

from agent.strategies.base import (
    Signal,
    SignalSink,
    SignalType,
    Strategy,
    StrategyServices,
)
from agent.strategies.registry import STRATEGIES, build_strategy, strategy

__all__ = [
    "STRATEGIES",
    "Signal",
    "SignalSink",
    "SignalType",
    "Strategy",
    "StrategyServices",
    "build_strategy",
    "strategy",
]

# Import concrete strategies so their decorators run and fill the registry.
# Keep this at the bottom to avoid circular imports.
import agent.strategies.tripple_screen  # noqa: E402, F401
import agent.strategies.tripple_screen_v2  # noqa: E402, F401
