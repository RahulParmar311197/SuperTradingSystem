"""MockBroker's balance and equity never moved, so the risk denominator
was a constant.

`_balance` and `_equity` were both assigned once in `__init__` and no fill
ever touched them. Every account with no connected broker resolves to this
broker, and four callers read `get_account()`:
`PaperTradingEngine._maybe_enter` (which sizes every paper and autonomous
entry from it), `POST /orders`, `POST /options/execute`, and
`GET /portfolio`/`portfolio_snapshots`.

Measured over 100 losing trades on a 100,000 account:

    cycle  0   realized      0.00   balance 100000.00   sized qty 151.5152
    cycle 99   realized -39118.55   balance  60881.45   sized qty  92.2446
    final      balance  60577.04 == 100000 + realized (-39422.96)

Before the fix every one of those 100 entries sized 151.5152 -- the
configured 0.5% of a balance that no longer existed, which by the end was
0.82% of what was left. Every percentage gate (`max_exposure_pct`,
`max_daily_loss_pct`, `max_weekly_loss_pct`, `max_strategy_allocation_pct`,
`max_correlated_exposure_pct`) is a fraction of that same denominator, so
all of them loosened in real terms as the account shrank -- the exact
opposite of what a risk limit is for. `GET /portfolio` also contradicted
itself, printing equity 100,000 beside a correct negative
`total_realized_pnl`.
"""

import uuid

import pytest

from app.brokers.base import OrderRequest
from app.database.models.strategy import Direction
from app.database.models.trading import OrderType

START = 100_000.0


def _broker():
    return __import__("app.brokers.mock", fromlist=["MockBroker"]).MockBroker(starting_balance=START)


async def _fill(broker, symbol, direction, quantity, price):
    broker.set_quote(symbol, ltp=price, is_market_print=False)
    return await broker.place_order(
        OrderRequest(
            idempotency_key=str(uuid.uuid4()),
            symbol=symbol,
            direction=direction,
            order_type=OrderType.MARKET,
            quantity=quantity,
        )
    )


# --- the finding ----------------------------------------------------------


async def test_a_realized_loss_moves_the_balance():
    """The headline. Buy 100 at 100, sell 100 at 50: the account is 5,000
    poorer, and `get_account()` has to say so."""
    broker = _broker()
    await _fill(broker, "AAA", Direction.LONG, 100.0, 100.0)
    await _fill(broker, "AAA", Direction.SHORT, 100.0, 50.0)

    account = await broker.get_account()
    assert account.balance == pytest.approx(START - 5_000.0)
    assert account.equity == pytest.approx(START - 5_000.0), "flat, so equity is just cash"


async def test_a_realized_gain_moves_the_balance_the_other_way():
    """The same mechanism in the direction that is not a loss -- a fix
    that only ever subtracted would pass the test above."""
    broker = _broker()
    await _fill(broker, "AAA", Direction.LONG, 100.0, 100.0)
    await _fill(broker, "AAA", Direction.SHORT, 100.0, 130.0)

    assert (await broker.get_account()).balance == pytest.approx(START + 3_000.0)


async def test_an_open_position_leaves_the_balance_alone():
    """Non-vacuity control, and the deliberate modelling choice recorded
    in `__init__`: this is cash, not margin. Opening a position does not
    debit the notional, because `max_exposure_pct` is a percentage OF the
    account and debiting would make the exposure gate tighten itself as
    positions open."""
    broker = _broker()
    await _fill(broker, "AAA", Direction.LONG, 100.0, 100.0)

    assert (await broker.get_account()).balance == pytest.approx(START)


async def test_equity_carries_the_open_position_even_though_cash_does_not():
    """The other half of that choice: unrealized P&L is in `equity`, which
    is what a real adapter's `equity` means (see UpstoxBroker.get_account).
    An account whose open position is 2,000 underwater is not still worth
    100,000."""
    broker = _broker()
    await _fill(broker, "AAA", Direction.LONG, 100.0, 100.0)
    broker.set_quote("AAA", ltp=80.0, is_market_print=False)

    account = await broker.get_account()
    assert account.balance == pytest.approx(START), "no fill, so no cash movement"
    assert account.equity == pytest.approx(START - 2_000.0)


async def test_a_partial_close_realizes_only_the_part_it_closed():
    """100 long at 100, sell 40 at 50: 2,000 realized, not 5,000. A fix
    that took the whole position's P&L on any reducing fill passes every
    test above and fails this one."""
    broker = _broker()
    await _fill(broker, "AAA", Direction.LONG, 100.0, 100.0)
    await _fill(broker, "AAA", Direction.SHORT, 40.0, 50.0)

    assert (await broker.get_account()).balance == pytest.approx(START - 2_000.0)


async def test_selling_through_zero_does_not_realize_the_flipped_side_twice():
    """100 long at 100, sell 150 at 50. 5,000 is realized on the 100 that
    closed; the 50 now short was opened at 50, not at the old 100 average.
    Buying it back at 50 must realize nothing. Without the cost-basis
    re-base this invents a second 2,500 gain out of the flip."""
    broker = _broker()
    await _fill(broker, "AAA", Direction.LONG, 100.0, 100.0)
    await _fill(broker, "AAA", Direction.SHORT, 150.0, 50.0)
    assert (await broker.get_account()).balance == pytest.approx(START - 5_000.0)

    await _fill(broker, "AAA", Direction.LONG, 50.0, 50.0)
    assert (await broker.get_account()).balance == pytest.approx(START - 5_000.0)


async def test_two_symbols_keep_separate_cost_bases_but_one_balance():
    """Cash is an account-level quantity; average price is not.

    The intermediate assertion is the load-bearing one. Checking only the
    end state would be symmetric under "realize nothing at all" -- the
    two legs cancel to exactly `START` either way -- so it would pass
    against the very bug this file is about.
    """
    broker = _broker()
    await _fill(broker, "AAA", Direction.LONG, 100.0, 100.0)
    await _fill(broker, "BBB", Direction.LONG, 100.0, 200.0)

    await _fill(broker, "AAA", Direction.SHORT, 100.0, 90.0)  # -1,000
    assert (await broker.get_account()).balance == pytest.approx(START - 1_000.0), (
        "BBB's open position must not be mixed into AAA's realized loss"
    )

    await _fill(broker, "BBB", Direction.SHORT, 100.0, 210.0)  # +1,000
    assert (await broker.get_account()).balance == pytest.approx(START)


# --- the restart half -----------------------------------------------------


async def test_restore_realized_pnl_rebuilds_cash_from_the_journal():
    """`_balance` lives in this process. A restart resets it to
    `starting_balance` -- which was harmless only while it never moved."""
    broker = _broker()
    broker.restore_realized_pnl(-40_000.0)

    assert (await broker.get_account()).balance == pytest.approx(60_000.0)


async def test_restore_realized_pnl_is_absolute_not_cumulative():
    """It is a rebuild from the whole journal, so calling it twice with
    the same total must not subtract twice. A `+=` implementation passes
    the test above and halves the account here."""
    broker = _broker()
    broker.restore_realized_pnl(-40_000.0)
    broker.restore_realized_pnl(-40_000.0)

    assert (await broker.get_account()).balance == pytest.approx(60_000.0)


async def test_restore_realized_pnl_respects_a_non_default_starting_balance():
    """It rebuilds from `starting_balance`, not from a hardcoded 100,000."""
    broker = __import__("app.brokers.mock", fromlist=["MockBroker"]).MockBroker(starting_balance=250_000.0)
    broker.restore_realized_pnl(-40_000.0)

    assert (await broker.get_account()).balance == pytest.approx(210_000.0)
