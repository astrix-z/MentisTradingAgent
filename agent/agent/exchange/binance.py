"""Binance Spot REST client built on ``binance-sdk-spot``.

This is the only module that talks to ``binance_sdk_spot``. It creates a Spot
client for a given network (testnet / live) using the credentials from
settings and exposes account queries such as the balance.

The SDK's REST methods are synchronous/blocking, so calls are executed through
``asyncio.to_thread`` so they never block the agent's event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from binance_common.configuration import ConfigurationRestAPI
from binance_sdk_spot import SPOT_REST_API_PROD_URL, SPOT_REST_API_TESTNET_URL, Spot

from agent.core.config import settings

logger = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    """Raised when a Binance request/connection fails."""


@dataclass(frozen=True)
class AssetBalance:
    asset: str
    free: float
    locked: float


def _network_credentials(
    network: str,
) -> tuple[str, str]:
    """Return (api_key, api_secret) for the given network from settings."""
    if network == "live":
        key, secret = settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET
    elif network == "testnet":
        key, secret = settings.BINANCE_TESTNET_API_KEY, settings.BINANCE_TESTNET_API_SECRET
    else:
        raise ExchangeError(f"unknown network: {network!r}")

    if not key or not secret:
        raise ExchangeError(
            f"Binance credentials for network={network!r} are not configured"
        )
    return key, secret


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


class BinanceSpot:
    """Thin async wrapper around a ``binance_sdk_spot.Spot`` REST client."""

    def __init__(self, spot: Spot) -> None:
        self._spot = spot

    @classmethod
    def connect(cls, network: str) -> BinanceSpot:
        """Build a client for ``network`` using the matching credentials."""
        api_key, api_secret = _network_credentials(network)
        base_path = (
            SPOT_REST_API_PROD_URL if network == "live" else SPOT_REST_API_TESTNET_URL
        )
        config = ConfigurationRestAPI(
            api_key=api_key,
            api_secret=api_secret,
            base_path=base_path,
        )
        return cls(Spot(config_rest_api=config))

    async def fetch_balances(self) -> list[AssetBalance]:
        """Return non-zero account balances (free + locked per asset)."""
        return await asyncio.to_thread(self._fetch_balances_sync)

    def _fetch_balances_sync(self) -> list[AssetBalance]:
        try:
            response = self._spot.rest_api.get_account(omit_zero_balances=True)
        except Exception as exc:  # noqa: BLE001 - SDK raises many typed errors
            raise ExchangeError(f"failed to fetch account balance: {exc}") from exc

        account = response.data()
        balances: list[AssetBalance] = []
        for item in account.balances or []:
            if item.asset is None:
                continue
            balances.append(
                AssetBalance(
                    asset=item.asset,
                    free=_to_float(item.free),
                    locked=_to_float(item.locked),
                )
            )
        return balances
