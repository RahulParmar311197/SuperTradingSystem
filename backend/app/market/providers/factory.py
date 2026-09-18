"""The one place that decides whether a real market-data provider exists.

Before this there was no such place, and that was the gap rather than a
detail of it: `UpstoxMarketData` existed, `backfill_candles` existed,
`resolve_instrument_key` existed -- and **nothing in `app/` constructed
the first or called the second**. Every layer of market-data ingestion was
present except the one that would have used them, so the only candles a
deployment could ever hold were whatever a test had inserted.

Configuration is process-level (`UPSTOX_DATA_ACCESS_TOKEN`) and creates no
`BrokerAccount` row, deliberately. `resolve_broker` picks the most recent
ACTIVE `BrokerAccount` for every order a user places and `_execution_mode_for`
stamps anything that is not a `MockBroker` as LIVE, so a row here would
route that user's real orders to Upstox as a side effect of wanting price
history. Keeping the credential out of the broker tables is what stops
market-data configuration from silently becoming execution configuration.

Worth repeating where the token is handled: Upstox issues no read-only
market-data tokens. The value this returns *can* place orders at Upstox
through any other client. What this module guarantees is that **this
system** offers no path from it to an order -- `UpstoxMarketData` has no
`place_order` by construction -- not that the credential is harmless if it
leaks.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings
from app.market.providers.upstox import UpstoxMarketData

logger = logging.getLogger("market.providers")


def market_data_provider() -> UpstoxMarketData | None:
    """The configured read-only provider, or `None` when there is none.

    `None` rather than a raise, because "no provider configured" is a
    legitimate deployment state -- paper trading against `MockBroker`
    needs none -- and the callers that do need one say so themselves with
    a message naming the remedy. A raise here would take down startup for
    a configuration that is fine.
    """
    token = (get_settings().upstox_data_access_token or "").strip()
    if not token:
        return None
    return UpstoxMarketData(token)


def market_data_provider_or_reason() -> tuple[UpstoxMarketData | None, str]:
    """The provider plus, when there isn't one, what to do about it.

    Split from `market_data_provider` so the reason is written once rather
    than at each call site in slightly different words -- the same
    duplication that an earlier round had to collapse into
    `signed_notionals_excluding` after breaking both copies at once went
    unnoticed.
    """
    provider = market_data_provider()
    if provider is not None:
        return provider, ""
    return None, (
        "No market-data provider is configured: set UPSTOX_DATA_ACCESS_TOKEN "
        "(process-level, deliberately not a BrokerAccount row -- see "
        "app/market/providers/factory.py). Without it this deployment has no "
        "path from a real feed into the candle store."
    )
