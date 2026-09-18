"""The autonomous loop traded the newest stored candle, however old it was.

`PaperTradingEngine` builds its `TradeRiskProposal` with
`market_data_age_seconds=0.0`, so the `market_data_fresh` check could not
fail on this path — and the `RiskEvent` row recorded it as a check that had
passed. Measured by driving the engine with a bar from January while the
clock said September:

    proposal market_data_age_seconds=0.0   market_data_fresh=True

`POST /orders` computes a real age from Redis and enforces a limit. This
path had the gate in name only, and it is the path that trades unattended.

It is the same shape as the `strategy_allocation=0.0` a previous round had
to fix on this very proposal, which is why `tests/paper/test_engine.py`
already carries a regression test for that one.

None of it is hypothetical: `_last_candle_seen` is in-memory, so a worker
restart clears it and the very next pass acts on the newest stored bar
whatever its date — and candles only reach the store when an operator runs
`POST /admin/backfill`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.market.timeframes import timeframe_to_minutes
from app.smc.types import Candle
from app.workers.auto_trade_worker import MAX_CANDLE_AGE_IN_BARS, candle_age_seconds

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _bar_at(minutes_ago: float) -> Candle:
    return Candle(NOW - timedelta(minutes=minutes_ago), 100.0, 101.0, 99.0, 100.5, 1000.0)


# --- the age a bar is actually judged on ----------------------------------


def test_a_bar_that_has_only_just_closed_is_not_late():
    """Behavioural proof. A 15m bar stamped at its open is not available
    until 15 minutes later, so the raw difference would call every freshly
    closed bar 900s stale and refuse the whole loop."""
    assert candle_age_seconds(_bar_at(15), "15m", NOW) == 0.0


def test_lateness_is_measured_beyond_the_bar_not_from_its_stamp():
    """Behavioural proof, and the distinction the whole gate rests on. A
    15m bar stamped 20 minutes ago is 5 minutes late, not 20."""
    assert candle_age_seconds(_bar_at(20), "15m", NOW) == pytest.approx(5 * 60)


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "1D"])
def test_the_bar_duration_comes_from_the_timeframe(timeframe):
    """Behavioural proof. The allowance has to scale with the timeframe --
    a daily bar is a day old the moment it closes."""
    minutes = timeframe_to_minutes(timeframe)
    assert candle_age_seconds(_bar_at(minutes), timeframe, NOW) == 0.0
    assert candle_age_seconds(_bar_at(minutes + 7), timeframe, NOW) == pytest.approx(7 * 60)


def test_a_bar_from_the_future_is_not_negatively_stale():
    """Control. Clock skew between the writer and this worker must not
    produce a negative age that would sail through any comparison."""
    assert candle_age_seconds(_bar_at(-120), "15m", NOW) == 0.0


def test_an_eight_month_old_bar_is_enormously_late():
    """Behavioural proof, and the measured case that found this. The bar
    the engine was handed during the original measurement was from
    2026-01-05 against a September clock."""
    january = Candle(datetime(2026, 1, 5, 9, 24, tzinfo=timezone.utc), 100.0, 101.0, 99.0, 100.5, 1000.0)

    age = candle_age_seconds(january, "15m", NOW)

    assert age > 200 * 24 * 3600, age
    assert age > timeframe_to_minutes("15m") * 60 * MAX_CANDLE_AGE_IN_BARS


# --- and the limit it is compared against ---------------------------------


def test_the_limit_is_bars_of_the_instruments_own_timeframe():
    """Control, and the one that pins why `RiskLimits.
    market_data_max_staleness_seconds` is not used here.

    That limit is 10 seconds and describes a *tick*, which is what
    `POST /orders` measures. A 15m bar is 900 seconds old the instant it
    closes, so applying the tick limit to bars would refuse every candle
    this loop has ever seen.
    """
    from app.risk.limits import RiskLimits

    tick_limit = RiskLimits().market_data_max_staleness_seconds
    bar_limit = timeframe_to_minutes("15m") * 60 * MAX_CANDLE_AGE_IN_BARS

    assert bar_limit > tick_limit
    # A bar that has only just closed must clear the bar limit, and would
    # not have cleared the tick one.
    just_closed = candle_age_seconds(_bar_at(15), "15m", NOW)
    assert just_closed <= bar_limit
    assert timeframe_to_minutes("15m") * 60 > tick_limit, (
        "if a bar were shorter than the tick limit this comparison would prove nothing"
    )


def test_the_allowance_is_more_than_one_bar_and_finite():
    """Control. One bar of slack would refuse a feed that is merely a
    little late, which this loop's own 60s polling cadence makes ordinary;
    an unbounded one would be no gate at all."""
    assert MAX_CANDLE_AGE_IN_BARS >= 2
    assert MAX_CANDLE_AGE_IN_BARS < 100
