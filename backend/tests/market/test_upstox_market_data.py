"""Real Upstox prices, paper execution — and the two conversions at the
boundary that fail silently if they are wrong.

The safety property first. `resolve_broker` routes every order to the most
recent ACTIVE `BrokerAccount`, and `_execution_mode_for` stamps anything
that is not a `MockBroker` as LIVE. So a market-data token stored as a
broker account would also send that user's real orders to Upstox.
`UpstoxMarketData` exists so the data credential lives somewhere that has
no ordering surface at all — not by convention, but because the class has
no such method to call.

Then the parsing. Upstox returns candles newest-first and stamped in IST;
this codebase is oldest-first and UTC everywhere. Neither mistake raises —
a reversed series just produces confident nonsense from every indicator,
and an IST clock time reaching a UTC reader is the bug behind both the
kill-zone and ICT-session-open regressions.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.brokers.base import Broker
from app.market.providers.upstox import (
    UpstoxMarketData,
    parse_candles,
    parse_ltp,
)

# Shaped as Upstox v2 documents it: newest-first rows of
# [timestamp, open, high, low, close, volume, open_interest], IST-stamped.
HISTORICAL_PAYLOAD = {
    "status": "success",
    "data": {
        "candles": [
            ["2026-09-16T09:45:00+05:30", 102.0, 103.0, 101.5, 102.5, 4000, 12],
            ["2026-09-16T09:30:00+05:30", 101.0, 102.5, 100.5, 102.0, 3000, 11],
            ["2026-09-16T09:15:00+05:30", 100.0, 101.0, 99.5, 101.0, 2000, 10],
        ]
    },
}


# --- the safety guarantee -------------------------------------------------


def test_the_market_data_client_is_not_a_broker():
    assert not issubclass(UpstoxMarketData, Broker)


@pytest.mark.parametrize("method", ["place_order", "modify_order", "cancel_order"])
def test_the_market_data_client_has_no_ordering_surface(method):
    # The whole point of the class. If someone later "helpfully" adds an
    # order method here, the data token becomes a trading token in this
    # system's own code paths and this fails.
    assert not hasattr(UpstoxMarketData, method)


def test_an_empty_token_is_refused_rather_than_deferred_to_a_401():
    with pytest.raises(ValueError):
        UpstoxMarketData(access_token="")


async def test_configuring_market_data_does_not_change_how_orders_route(require_infra):
    """Control, end to end: with the data token set and no BrokerAccount,
    execution must still resolve to `MockBroker` — i.e. paper."""
    import uuid

    from sqlalchemy import delete

    from app.brokers.mock import MockBroker
    from app.database.models.users import User
    from app.database.session import async_session_factory
    from app.trading.broker_resolver import resolve_broker

    async with async_session_factory() as db:
        user = User(email=f"md-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", name="MD")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        try:
            broker, broker_account_id = await resolve_broker(db, user)
            assert isinstance(broker, MockBroker)
            assert broker_account_id is None
        finally:
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()


# --- candle order ---------------------------------------------------------


def test_candles_come_back_oldest_first():
    # Upstox sends newest-first; everything downstream reads `candles[-1]`
    # as "now".
    candles = parse_candles(HISTORICAL_PAYLOAD)
    assert [c.close for c in candles] == [101.0, 102.0, 102.5]
    assert candles == sorted(candles, key=lambda c: c.timestamp)


def test_the_last_candle_is_the_most_recent_one():
    # Stated separately because this is the assumption every consumer
    # actually makes, and it is the one a reversed series breaks.
    assert parse_candles(HISTORICAL_PAYLOAD)[-1].timestamp == datetime(2026, 9, 16, 4, 15, tzinfo=timezone.utc)


# --- timezone -------------------------------------------------------------


def test_ist_timestamps_carry_a_utc_offset_not_an_ist_one():
    """The offset, not just the instant.

    An earlier version of this test asserted only
    `== datetime(2026, 9, 16, 3, 45, tzinfo=utc)` and passed even with the
    conversion removed — because `09:15+05:30` and `03:45+00:00` are the
    same instant, and aware datetimes compare by instant. It proved
    nothing, and an injection run caught it.

    The offset is what actually matters downstream: `.hour` on the
    IST-stamped value is 9 and on the UTC one is 3, and
    `app/market/aggregation.py`, `app/smc/liquidity.py` and
    `app/ict/opening_range.py` all read `.hour`/`.date()`/`.weekday()`
    straight off the timestamp. Carrying an IST offset into them is the
    kill-zone regression again, one layer up.
    """
    candle = parse_candles(HISTORICAL_PAYLOAD)[0]
    assert candle.timestamp.utcoffset() == timedelta(0)
    assert candle.timestamp.hour == 3  # not 9
    assert candle.timestamp == datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc)


def test_every_candle_is_utc_aware():
    assert all(c.timestamp.utcoffset() == timedelta(0) for c in parse_candles(HISTORICAL_PAYLOAD))


def test_a_naive_timestamp_is_read_as_utc_not_as_machine_local_time():
    # `astimezone()` on a naive datetime assumes the machine's zone, which
    # a UTC-configured CI would never catch.
    payload = {"data": {"candles": [["2026-09-16T03:45:00", 1.0, 2.0, 0.5, 1.5, 10, 0]]}}
    assert parse_candles(payload)[0].timestamp == datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc)


# --- robustness -----------------------------------------------------------


def test_open_interest_is_dropped_rather_than_guessed_at():
    # `Candle` has nowhere to put it; the row is 7 wide and we read 6.
    candle = parse_candles(HISTORICAL_PAYLOAD)[0]
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == (100.0, 101.0, 99.5, 101.0, 2000.0)


@pytest.mark.parametrize(
    "rows",
    [
        [["2026-09-16T09:15:00+05:30", 1.0, 2.0]],           # too short
        [["not-a-timestamp", 1.0, 2.0, 0.5, 1.5, 10, 0]],    # unparseable stamp
        ["not-even-a-row"],                                   # not a sequence
    ],
)
def test_a_malformed_row_is_skipped_rather_than_taking_the_batch_down(rows):
    # One bad row in a day of candles must not cost the whole fetch.
    assert parse_candles({"data": {"candles": rows}}) == []


def test_a_good_row_survives_a_bad_neighbour():
    # Control for the test above: skipping must be per-row, not all-or-nothing.
    payload = {
        "data": {
            "candles": [
                ["broken", 1.0, 2.0, 0.5, 1.5, 10, 0],
                ["2026-09-16T09:15:00+05:30", 100.0, 101.0, 99.5, 101.0, 2000, 10],
            ]
        }
    }
    assert len(parse_candles(payload)) == 1


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": {"candles": []}}])
def test_an_empty_response_is_no_candles_not_a_crash(payload):
    assert parse_candles(payload) == []


# --- ltp ------------------------------------------------------------------


def test_ltp_reads_the_single_entry_whatever_upstox_keys_it_by():
    # The response key ("NSE_EQ:INFY") is not the request key
    # ("NSE_EQ|INE009A01021") and its spelling has moved between versions,
    # so a one-instrument request takes the only entry there is.
    payload = {"data": {"NSE_EQ:INFY": {"last_price": 1542.5}}}
    assert parse_ltp(payload, "NSE_EQ|INE009A01021") == 1542.5


def test_ltp_matches_by_key_when_several_come_back():
    payload = {"data": {"NSE_EQ:INFY": {"last_price": 1.0}, "NSE_EQ|INE002A01018": {"last_price": 2.0}}}
    assert parse_ltp(payload, "NSE_EQ|INE002A01018") == 2.0


@pytest.mark.parametrize("payload", [{}, {"data": {}}, {"data": {"X": {}}}])
def test_ltp_is_none_when_there_is_nothing_to_read(payload):
    # None, not 0.0: a missing price must never look like a real one.
    assert parse_ltp(payload, "X") is None


# --- request guards -------------------------------------------------------


async def test_an_unsupported_interval_is_named_rather_than_sent():
    client = UpstoxMarketData(access_token="t")
    with pytest.raises(ValueError, match="5minute"):
        await client.get_historical_candles("NSE_EQ|X", "5minute", date(2026, 9, 1), date(2026, 9, 16))
    await client.aclose()


async def test_a_backwards_date_range_is_refused():
    client = UpstoxMarketData(access_token="t")
    with pytest.raises(ValueError):
        await client.get_historical_candles("NSE_EQ|X", "day", date(2026, 9, 16), date(2026, 9, 1))
    await client.aclose()
