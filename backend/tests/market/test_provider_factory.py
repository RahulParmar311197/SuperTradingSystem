"""The one place that decides whether a real market-data provider exists.

Before this there was no such place, and that absence *was* the gap:
`UpstoxMarketData`, `backfill_candles` and `resolve_instrument_key` all
existed, and nothing in `app/` constructed the first or called the second.
Every layer of market-data ingestion was present except the one that would
have used them.
"""

import pytest

from app.market.providers.factory import market_data_provider, market_data_provider_or_reason
from app.market.providers.upstox import UpstoxMarketData

pytestmark = pytest.mark.asyncio


def _with_token(monkeypatch, token):
    import app.market.providers.factory as factory

    class _Settings:
        upstox_data_access_token = token

    monkeypatch.setattr(factory, "get_settings", lambda: _Settings())


async def test_a_configured_token_yields_a_read_only_client(monkeypatch):
    """Behavioural proof. The client this returns must be the one that
    structurally cannot place an order -- market-data configuration must
    never become execution configuration."""
    _with_token(monkeypatch, "a-token")
    provider = market_data_provider()

    assert isinstance(provider, UpstoxMarketData)
    for forbidden in ("place_order", "modify_order", "cancel_order"):
        assert not hasattr(provider, forbidden), (
            f"the market-data client grew a {forbidden!r} surface; the whole point of "
            "UpstoxMarketData is that the token it holds has no path to an exchange here"
        )


async def test_no_token_is_no_provider_rather_than_a_crash(monkeypatch):
    """Control. "No provider configured" is a legitimate deployment state
    -- paper trading against MockBroker needs none -- so this must not
    raise and take down startup for a configuration that is fine."""
    _with_token(monkeypatch, None)
    assert market_data_provider() is None


async def test_a_blank_token_counts_as_no_token(monkeypatch):
    """Control. An env var set to empty or whitespace is the commonest way
    to *think* you configured something. `UpstoxMarketData` rejects a blank
    token by raising, so without the strip this would crash the caller
    instead of reporting the misconfiguration."""
    _with_token(monkeypatch, "   ")
    assert market_data_provider() is None


async def test_the_missing_provider_reason_names_the_remedy(monkeypatch):
    """Behavioural proof. A caller that needs a provider surfaces this
    string to an operator, so it has to say what to set -- not merely that
    something is absent."""
    _with_token(monkeypatch, None)
    provider, reason = market_data_provider_or_reason()

    assert provider is None
    assert "UPSTOX_DATA_ACCESS_TOKEN" in reason
    assert "BrokerAccount" in reason, "must say why this is not a broker connection"


async def test_a_configured_provider_carries_no_reason(monkeypatch):
    """Control. The reason must be empty exactly when there is a provider,
    or a caller that branches on it reports a problem that does not exist."""
    _with_token(monkeypatch, "a-token")
    provider, reason = market_data_provider_or_reason()

    assert provider is not None
    assert reason == ""
