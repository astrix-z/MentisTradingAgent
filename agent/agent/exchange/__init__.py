"""Exchange access, built directly on ``binance_sdk_spot``.

Two thin adapters make up the whole exchange layer:

* :class:`~agent.exchange.rest.ExchangeRest` — async facade over the REST API;
* :class:`~agent.exchange.streams.MarketDataStreams` — async facade over the
  market-data websocket streams.

Neither defines its own order/quote/balance models: they return the SDK's models
so the agent never duplicates what the library already provides.
"""

from agent.exchange.rest import (
    ExchangeError,
    ExchangeRest,
    Network,
    balance_map,
    balance_snapshot,
)
from agent.exchange.streams import MarketDataStreams

__all__ = [
    "ExchangeError",
    "ExchangeRest",
    "MarketDataStreams",
    "Network",
    "balance_map",
    "balance_snapshot",
]
