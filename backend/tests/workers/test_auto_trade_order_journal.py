"""Round 178: the autonomous path submitted real orders and journaled none.

`persist_order` had three callers before this round and all three were
request handlers. `AutoTradeSupervisor` reached none of them, so a loop
running unattended -- the one path with nobody watching it -- left
nothing in `orders` for `GET /admin/orders` (blueprint §116) to show.
Measured on a supervisor run that opened and closed a position on each
of two instruments: 2 positions, 2 `trades` rows, **0** order rows.

A `trades` row is not a substitute. It records a completed round trip,
so it cannot show an order that was submitted and refused, and it
appears only once the position is closed.

The second test here is the one that costs something to get wrong.
`_maybe_enter` built its idempotency key from
`{account}:{strategy}:{timestamp}` with no symbol, while the closing key
two methods down always named one. The supervisor runs one engine per
(strategy, instrument) sharing an `account_id`, and two instruments on
one timeframe necessarily share bar timestamps -- so one strategy
entering on two instruments on the same bar computed ONE key for two
different orders. That was invisible while nothing persisted them.
`persist_order` selects by `idempotency_key` and UPDATEs the row it
finds, so wiring journaling up without fixing the key first would have
folded the second instrument's entry into the first one's row: one order
row claiming one instrument, for two real fills on two instruments.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text

from app.auth.security import hash_password
from app.database.models.instruments import Instrument, MarketType
from app.database.models.strategy import Direction
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import (
    ExecutionMode,
    Order,
    OrderStatus,
    OrderType,
    Position,
)
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.smc.types import Candle
from app.workers.auto_trade_worker import AutoTradeSupervisor
from tests.instrument_cleanup import purge_instrument

# The same bullish sweep+FVG dataset every other execution test uses:
# proven to match, enter on bar 8 and run to target on bar 9.
SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces into the FVG -> entry
    (104, 130, 104, 128),  # runs hard to target
]

STRATEGY_DEFINITION = {
    "name": "Bullish FVG retest",
    "market": "TESTSYM",
    "timeframe": "15m",
    "direction": "bullish",
    "conditions": [{"type": "fvg", "direction": "bullish"}],
    "entry": {"type": "fvg_retest"},
    "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
}

# See the note in tests/workers/test_auto_trade_worker.py: the equity
# liquidity gate caps an order at a share of what the instrument trades,
# and these setups size to 250-500 shares.
LIQUID_BAR_VOLUME = 50_000.0


def _recent_start() -> datetime:
    """Anchor the series so its newest bar is inside the freshness gate."""
    return datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)


def _candles() -> list[Candle]:
    start = _recent_start()
    return [
        Candle(start + timedelta(minutes=i), o, h, l, c, LIQUID_BAR_VOLUME)
        for i, (o, h, l, c) in enumerate(SETUP)
    ]


async def _instrument() -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"OJ{uuid.uuid4().hex[:8].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _auto_trading_user(market: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"ordjournal-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Order Journal",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
            auto_trading_risk_per_trade_pct=1.0,
            auto_trading_max_positions=5,
        )
        db.add(user)
        await db.flush()
        strategy = StrategyRow(
            user_id=user.id,
            name="Bullish FVG retest",
            definition={**STRATEGY_DEFINITION, "market": market},
            is_active=True,
            eligible_for_auto_trading=True,
        )
        db.add(strategy)
        await db.commit()
        return user.id, strategy.id


async def _cleanup(user_id: uuid.UUID, instruments: list[uuid.UUID]) -> None:
    async with async_session_factory() as db:
        await db.execute(
            text("DELETE FROM order_events WHERE order_id IN (SELECT id FROM orders WHERE user_id = :u)"),
            {"u": user_id},
        )
        await db.execute(text("DELETE FROM orders WHERE user_id = :u"), {"u": user_id})
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        for table in ("notifications", "audit_logs", "risk_events"):
            await db.execute(text(f"DELETE FROM {table} WHERE user_id = :u"), {"u": user_id})
        await db.execute(delete(StrategyRow).where(StrategyRow.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        # Scoped by instrument, not by user -- see tests/instrument_cleanup.py.
        for instrument_id in instruments:
            await purge_instrument(db, instrument_id)
        await db.commit()


async def _orders_of(user_id: uuid.UUID) -> list[Order]:
    async with async_session_factory() as db:
        return list(
            (
                await db.execute(
                    select(Order).where(Order.user_id == user_id).order_by(Order.created_at)
                )
            ).scalars().all()
        )


async def _drive(supervisor: AutoTradeSupervisor, instrument_ids: list[uuid.UUID]) -> None:
    """Feed the whole setup, one bar per pass, to every instrument at once.

    One `run_once()` per bar rather than per instrument: that is what puts
    two engines on the SAME bar timestamp, which is the condition the
    idempotency key has to survive.
    """
    for candle in _candles():
        async with async_session_factory() as db:
            for instrument_id in instrument_ids:
                await upsert_candles(db, instrument_id, "15m", [candle])
        await supervisor.run_once()


async def test_an_auto_traded_entry_and_exit_each_leave_an_order_row(require_infra):
    instrument = await _instrument()
    user_id, strategy_id = await _auto_trading_user(instrument.symbol)
    try:
        await _drive(AutoTradeSupervisor(timeframe="15m"), [instrument.id])

        orders = await _orders_of(user_id)
        # The measurement this test exists for: this was 0.
        assert len(orders) == 2, f"expected an entry and an exit order row, got {len(orders)}"

        entry, exit_ = orders
        assert entry.direction is Direction.LONG
        assert exit_.direction is Direction.SHORT
        for order in orders:
            # Measured, not assumed. `ExecutionEngine.submit`
            # (app/trading/execution.py:72) moves ANY fully-filled order
            # on to MONITORING, the closing one included -- so this is
            # what a journaled paper/auto fill looks like, and asserting
            # FILLED here would be asserting something that never happens.
            assert order.status is OrderStatus.MONITORING
            # Not LIVE: round 51's rule, a MockBroker fill is PAPER.
            assert order.execution_mode is ExecutionMode.PAPER
            assert order.instrument_id == instrument.id
            assert order.strategy_id == strategy_id
            assert order.strategy_version == 1
            # `persist_order`'s own docstring: None for paper/backtest.
            assert order.broker_account_id is None
            # Both were MARKET orders the engine sized itself.
            assert order.order_type is OrderType.MARKET
            assert float(order.quantity) > 0

        # The position really did round-trip, so these orders describe
        # fills rather than an entry that never closed.
        async with async_session_factory() as db:
            trades = (
                await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))
            ).scalars().all()
        assert len(trades) == 1
        assert entry.quantity == exit_.quantity
    finally:
        await _cleanup(user_id, [instrument.id])


async def test_two_instruments_entering_on_the_same_bar_do_not_share_one_order_row(require_infra):
    """The symbol in the entry idempotency key, measured end to end.

    Both instruments carry identical candles, so both engines enter on the
    same bar timestamp under one `account_id` and one strategy name. With
    the symbol absent from the key those two entries collide, and
    `persist_order` -- which selects on the key and UPDATEs what it finds
    -- writes one row for both.
    """
    first, second = await _instrument(), await _instrument()
    user_id, _ = await _auto_trading_user(first.symbol)
    try:
        await _drive(AutoTradeSupervisor(timeframe="15m"), [first.id, second.id])

        orders = await _orders_of(user_id)
        assert len(orders) == 4, f"expected entry+exit on each of two instruments, got {len(orders)}"

        keys = {order.idempotency_key for order in orders}
        assert len(keys) == 4, f"orders collapsed onto a shared key: {sorted(keys)}"

        entries = [order for order in orders if order.direction is Direction.LONG]
        assert len(entries) == 2
        # The point of the whole fix: each entry names the instrument it
        # was actually submitted for, and they are different instruments.
        assert {order.instrument_id for order in entries} == {first.id, second.id}
        # And the key itself says so, which is what makes the two distinct.
        # `{account}:{strategy}:{symbol}:{timestamp}` -- index 2, not -2:
        # the ISO timestamp at the end carries colons of its own.
        assert {first.symbol, second.symbol} == {
            order.idempotency_key.split(":")[2] for order in entries
        }

        exits = [order for order in orders if order.direction is Direction.SHORT]
        assert {order.instrument_id for order in exits} == {first.id, second.id}
    finally:
        await _cleanup(user_id, [first.id, second.id])
