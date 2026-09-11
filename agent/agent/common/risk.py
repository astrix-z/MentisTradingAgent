"""Risk gate: limits applied between a decision and an order hitting the book.

Strategies and the position protector propose orders, but nothing is placed
until the risk gate approves it. The gate guards the whole account, not a single
strategy: number of resting orders, per-symbol and total notional, the number of
concurrently held symbols and a daily-loss cap.

The gate reads the agent's current state from ``Portfolio`` (positions) and
``MarketData`` (last prices) and, optionally, a provider of the open orders on
the exchange (defaulting to "skip the count check" when none is supplied).
"""
