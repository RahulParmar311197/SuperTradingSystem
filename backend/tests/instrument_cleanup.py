"""Delete an `Instrument` together with every row that references it.

WHY THIS EXISTS. `instruments` has ten incoming foreign keys and no
cascade delete anywhere, so `DELETE FROM instruments` succeeds only once
nothing points at it. A teardown that deletes only the rows *its own
test* created therefore works right up until some other actor has
written a row against the same instrument -- and two actors in this
suite write against **every active instrument**, not against one they
were handed:

  - `ScannerWorker` (app/workers/scanner_worker.py:48) selects every
    `Instrument.active.is_(True)` and writes `Signal` and `Setup` rows
    keyed by `instrument_id` alone. Those rows belong to no user, so no
    user-scoped delete can ever reach them.
  - `AutoTradeSupervisor.run_once` (app/workers/auto_trade_worker.py:214)
    does the same select and pairs *every eligible user's* strategies
    with it, so another test's user can hold `positions`, `orders`,
    `trades` and `order_events` on an instrument this test created.

WHAT THAT COST, MEASURED. The delete raising is not the damage -- the
rollback is. A teardown that deletes user rows and the instrument in one
transaction loses **all** of it when the instrument delete raises, so the
instrument survives *with its candles*, and a 15m bar stays inside
`MAX_CANDLE_AGE_IN_BARS` for a full hour. For that hour the leaked
instrument is live, tradeable input for every later test: the supervisor
pairs it with each new test's brand-new user and, because round 79 seeds
a fresh engine with the instrument's whole stored history, fires an entry
on the very first bar that test feeds.

Measured against three leaked `XPA*` instruments left by
`tests/api/test_cross_process_trade_lock.py`, driving a brand-new user
with a brand-new instrument of its own:

    bar 0  results for my user: 4 -- my instrument order_created=False
           and *three foreign instruments* order_created=True
    bars 1-9  my instrument order_created=False on every bar,
           including bar 8, the entry bar

The three stray fills consumed the account-wide book the supervisor
shares per user, so the test's own entry never filled and its exit never
came: `tests/workers/test_auto_trade_*` failed 25 tests in a row with
`assert closed_pnl is not None` -> `assert None is not None`, with
nothing wrong in the code under test. Reverting the production diff left
the failures in place, which is what identified the residue rather than
the change.

So: delete child rows first, every one of them, scoped by instrument and
not by user.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from app.database.models.instruments import Instrument
from app.database.models.market import Candle as CandleRow
from app.database.models.market import Tick as TickRow
from app.database.models.strategy import Setup as SetupRow
from app.database.models.strategy import Signal as SignalRow
from app.database.models.trading import Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow


async def purge_instrument(db, instrument_id: uuid.UUID) -> None:
    """Remove every row referencing `instrument_id`, then the instrument.

    Does not commit -- the caller decides the transaction boundary, so
    this can be folded into a teardown that is already deleting a user.

    Order is child-rows-first and is load-bearing: `order_events` points
    at `orders`, and `trades` points at both `positions` and
    `instruments`.
    """
    order_ids = (
        await db.execute(select(Order.id).where(Order.instrument_id == instrument_id))
    ).scalars().all()
    if order_ids:
        await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
    await db.execute(delete(TradeRow).where(TradeRow.instrument_id == instrument_id))
    await db.execute(delete(Order).where(Order.instrument_id == instrument_id))
    await db.execute(delete(Position).where(Position.instrument_id == instrument_id))
    await db.execute(delete(SignalRow).where(SignalRow.instrument_id == instrument_id))
    await db.execute(delete(SetupRow).where(SetupRow.instrument_id == instrument_id))
    await db.execute(delete(TickRow).where(TickRow.instrument_id == instrument_id))
    await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
    await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
