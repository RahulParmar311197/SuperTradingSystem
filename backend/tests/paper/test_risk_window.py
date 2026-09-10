"""`RiskWindow` is driven by a clock that can move backwards.

`AutoTradeSupervisor` shares one `RiskWindow` per *user*
(`app/workers/auto_trade_worker.py`, `self._risk_windows`) across one
`PaperTradingEngine` per (strategy, instrument) triple -- that sharing is
the whole point of the object, since `max_trades_per_day` and the
daily/weekly loss limits are account-wide. But `on_candle` rolls the
window with `candle.timestamp`, so the shared window is driven by N
different instruments' feeds. Those are not synchronised with each other,
`run_once` selects active instruments with no `ORDER BY`, and an
instrument can simply be behind: illiquid and yet to print a bar today,
mid-backfill, or halted.

`roll` compared with `!=`, so an older timestamp was indistinguishable
from a new day and zeroed the account's spent risk budget mid-session.

The sibling path is safe and stays untouched: `_UserTradingStack`
(`app/api/orders.py`) is rolled with `datetime.now(timezone.utc)`, one
monotonic wall clock per account.

Why the existing tests could not see this:

* `tests/workers/test_auto_trade_worker.py::
  test_supervisor_caps_trades_per_day_account_wide_across_instruments`
  is the one two-instrument fixture, and it writes *the same* `candles[i]`
  object to both instruments -- so both engines always roll with an
  identical timestamp and `roll` is a permanent no-op.
* `tests/paper/test_engine.py::
  test_paper_engine_resets_daily_and_weekly_counters_at_boundaries` and
  `tests/api/test_orders.py::test_risk_window_rolls_at_day_and_week_boundaries`
  only ever advance the clock, and assert *that* the counters reset. That
  a *backwards* step must not reset them is a contract neither states.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.paper.engine import PaperTradingEngine, RiskWindow
from app.smc.types import Candle
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition

# A Monday, matching tests/smc/conftest.py.
MONDAY = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)


def _spent_window() -> RiskWindow:
    """A window with the account's daily/weekly budget already spent."""
    window = RiskWindow()
    window.roll(MONDAY)
    window.trades_today = 4
    window.daily_pnl = -1500.0
    window.weekly_pnl = -3200.0
    return window


def test_an_older_timestamp_does_not_reset_the_daily_counters():
    # Regression test: pre-fix `roll` used `today != self.risk_day`, so a
    # lagging instrument's bar was a "boundary crossing" and handed the
    # account back its whole `max_trades_per_day` / `daily_loss_limit`
    # budget in the middle of the session it had already spent it in.
    window = _spent_window()

    window.roll(MONDAY - timedelta(days=1))

    assert window.trades_today == 4
    assert window.daily_pnl == -1500.0
    assert window.weekly_pnl == -3200.0


def test_a_lagging_roll_does_not_rearm_the_next_forward_roll():
    # The half that a bare `>` on the reset would still get wrong, and the
    # reason `risk_day`/`risk_week` are high-water marks rather than "the
    # last timestamp seen": if the lagging roll rewound the mark, the very
    # next bar from the up-to-date instrument would compare against the
    # rewound value, read as a fresh day and reset after all. With N
    # instruments this alternates, so the counters are wiped repeatedly.
    window = _spent_window()

    window.roll(MONDAY - timedelta(days=1))
    window.roll(MONDAY + timedelta(hours=1))

    assert window.risk_day == MONDAY.date()
    assert window.trades_today == 4
    assert window.daily_pnl == -1500.0


def test_an_older_timestamp_does_not_reset_the_weekly_counter():
    window = _spent_window()

    # The previous ISO week, which pre-fix reset `weekly_pnl` as well.
    window.roll(MONDAY - timedelta(days=7))
    assert window.weekly_pnl == -3200.0

    window.roll(MONDAY + timedelta(hours=1))
    assert window.weekly_pnl == -3200.0
    assert window.risk_week == MONDAY.isocalendar()[:2]


def test_a_later_day_still_resets_the_daily_counters():
    # The control: the boundary reset this object exists for must survive.
    window = _spent_window()

    window.roll(MONDAY + timedelta(days=1))

    assert window.trades_today == 0
    assert window.daily_pnl == 0.0
    # Still the same ISO week, so the weekly figure is untouched.
    assert window.weekly_pnl == -3200.0
    assert window.risk_day == (MONDAY + timedelta(days=1)).date()


def test_a_later_week_still_resets_the_weekly_counter():
    window = _spent_window()

    window.roll(MONDAY + timedelta(days=7))

    assert window.weekly_pnl == 0.0
    assert window.trades_today == 0
    assert window.daily_pnl == 0.0


def test_the_first_roll_never_resets_a_fresh_window():
    # `None` marks still mean "no window established yet" -- a freshly
    # constructed window adopts whatever clock it is first shown, forwards
    # or backwards, without treating it as a boundary.
    window = RiskWindow(trades_today=2, daily_pnl=-400.0, weekly_pnl=-900.0)

    window.roll(MONDAY)

    assert window.trades_today == 2
    assert window.daily_pnl == -400.0
    assert window.weekly_pnl == -900.0
    assert window.risk_day == MONDAY.date()


def _strategy() -> StrategyDefinition:
    return StrategyDefinition(
        name="Bullish FVG retest",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[Condition(type=ConditionType.FVG, direction="bullish")],
        entry=EntryConfig(type="fvg_retest"),
        risk=RiskConfig(risk_percent=1.0, minimum_rr=2.0),
    )


def _bar(timestamp: datetime) -> Candle:
    return Candle(timestamp=timestamp, open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0)


@pytest.mark.asyncio
async def test_a_lagging_engine_does_not_clear_its_siblings_spent_budget():
    # The supervisor's actual shape, through the real entry point:
    # `on_candle` rolls the shared window as its *first* statement, before
    # any position or history guard, so an instrument that never trades
    # still resets the account. Two engines, one window, one of them
    # behind.
    window = _spent_window()
    lagging = PaperTradingEngine(
        _strategy(), symbol="LAGGY", starting_balance=100_000, risk_window=window
    )
    up_to_date = PaperTradingEngine(
        _strategy(), symbol="TESTSYM", starting_balance=100_000, risk_window=window
    )

    await lagging.on_candle(_bar(MONDAY - timedelta(days=1)))

    # Read through the sibling engine: the counters are shared state, and
    # this is the engine whose next signal the limits are supposed to gate.
    assert up_to_date.trades_today == 4
    assert up_to_date.daily_pnl == -1500.0

    await up_to_date.on_candle(_bar(MONDAY + timedelta(minutes=15)))

    assert up_to_date.trades_today == 4
    assert up_to_date.daily_pnl == -1500.0
    assert window.risk_day == MONDAY.date()
