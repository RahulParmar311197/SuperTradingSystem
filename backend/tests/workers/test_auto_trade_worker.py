import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.database.models.notifications import Notification, NotificationType
from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.risk.engine import RiskEngine
from app.risk.limits import RiskDecision, RiskDecisionResult
from app.smc.types import Candle
from app.workers.auto_trade_worker import AutoTradeSupervisor

# Same bullish sweep+FVG dataset already proven to produce a match and then
# run to target in tests/strategy and tests/backtest.
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


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_supervisor_skips_users_with_auto_trading_disabled(db_instrument):
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"autotrade-off-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Auto Trade Off",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=False,  # explicitly off
        )
        db.add(user)
        await db.flush()
        strategy = StrategyRow(user_id=user.id, name="s", definition=STRATEGY_DEFINITION, is_active=True, eligible_for_auto_trading=True)
        db.add(strategy)
        await db.commit()
        user_id = user.id

    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        results = await supervisor.run_once()
        assert not any(r["user_id"] == str(user_id) for r in results)
    finally:
        await _cleanup(user_id)


async def test_supervisor_skips_strategy_not_marked_eligible(db_instrument):
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"autotrade-ineligible-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Auto Trade Ineligible Strategy",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
        )
        db.add(user)
        await db.flush()
        strategy = StrategyRow(
            user_id=user.id, name="s", definition=STRATEGY_DEFINITION, is_active=True, eligible_for_auto_trading=False
        )
        db.add(strategy)
        await db.commit()
        user_id = user.id

    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        results = await supervisor.run_once()
        assert not any(r["user_id"] == str(user_id) for r in results)
    finally:
        await _cleanup(user_id)


async def test_supervisor_opens_and_journals_a_trade_end_to_end(db_instrument):
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(SETUP)]

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"autotrade-on-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Auto Trade On",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
            auto_trading_risk_per_trade_pct=1.0,
        )
        db.add(user)
        await db.flush()
        strategy = StrategyRow(
            user_id=user.id,
            name="Bullish FVG retest",
            definition={**STRATEGY_DEFINITION, "market": db_instrument.symbol},
            is_active=True,
            eligible_for_auto_trading=True,
        )
        db.add(strategy)
        await db.commit()
        user_id = user.id
        strategy_id = strategy.id

    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        opened = False
        closed_pnl = None
        for i in range(len(candles)):
            async with async_session_factory() as db:
                await upsert_candles(db, db_instrument.id, "15m", [candles[i]])
            results = await supervisor.run_once()
            for r in results:
                if r["user_id"] == str(user_id):
                    opened = opened or r["order_created"]
                    if r["closed_pnl"] is not None:
                        closed_pnl = r["closed_pnl"]

        assert opened is True
        assert closed_pnl is not None
        assert closed_pnl > 0

        async with async_session_factory() as db:
            trades = (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
            notifications = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()

        assert len(trades) == 1
        assert trades[0].strategy_id == strategy_id
        assert float(trades[0].pnl) == pytest.approx(closed_pnl, rel=1e-6)
        assert trades[0].journal.get("symbol") == db_instrument.symbol
        assert len(notifications) == 1
    finally:
        await _cleanup(user_id)


async def test_supervisor_picks_up_strategy_edited_after_engine_cached(db_instrument):
    # Regression test: `_process` cached one `PaperTradingEngine` per
    # (user, strategy, instrument) and only ever used the freshly re-parsed
    # `StrategyDefinition` argument `if engine is None` -- on a cache hit it
    # was silently discarded, so an engine kept evaluating every future
    # candle against whatever DSL existed the moment it was first built,
    # even after the user edited the strategy (PUT /strategies/{id} bumps
    # `version` and rewrites `definition`). Prove the fix: start with a
    # strategy that can never match this bullish dataset (requires a
    # bearish setup), let the supervisor cache an engine against it on the
    # first candle, then edit the strategy in place to the real bullish
    # definition and bump its version -- the rest of the same dataset
    # should still open and close a trade, which is only possible if the
    # cached engine actually picked up the edit.
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(SETUP)]

    unmatchable_definition = {
        **STRATEGY_DEFINITION,
        "market": db_instrument.symbol,
        "direction": "bearish",
        "conditions": [{"type": "fvg", "direction": "bearish"}],
    }

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"autotrade-edit-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Auto Trade Edited Strategy",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
            auto_trading_risk_per_trade_pct=1.0,
        )
        db.add(user)
        await db.flush()
        strategy = StrategyRow(
            user_id=user.id,
            name="Bullish FVG retest",
            definition=unmatchable_definition,
            is_active=True,
            eligible_for_auto_trading=True,
        )
        db.add(strategy)
        await db.commit()
        user_id = user.id
        strategy_id = strategy.id

    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")

        # First candle: caches an engine bound to the unmatchable strategy.
        async with async_session_factory() as db:
            await upsert_candles(db, db_instrument.id, "15m", [candles[0]])
        await supervisor.run_once()
        assert (str(user_id), str(strategy_id), str(db_instrument.id)) in supervisor._engines

        # The user edits the strategy to the real, matchable definition.
        async with async_session_factory() as db:
            strategy_row = await db.get(StrategyRow, strategy_id)
            strategy_row.definition = {**STRATEGY_DEFINITION, "market": db_instrument.symbol}
            strategy_row.version = 2
            await db.commit()

        opened = False
        closed_pnl = None
        for i in range(1, len(candles)):
            async with async_session_factory() as db:
                await upsert_candles(db, db_instrument.id, "15m", [candles[i]])
            results = await supervisor.run_once()
            for r in results:
                if r["user_id"] == str(user_id):
                    opened = opened or r["order_created"]
                    if r["closed_pnl"] is not None:
                        closed_pnl = r["closed_pnl"]

        assert opened is True
        assert closed_pnl is not None and closed_pnl > 0

        async with async_session_factory() as db:
            trades = (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
        assert len(trades) == 1
        assert trades[0].strategy_version == 2
    finally:
        await _cleanup(user_id)


async def test_supervisor_notifies_on_risk_rejected_entry(db_instrument, monkeypatch):
    # Regression test: `PaperTradingEngine.on_candle` returns
    # `risk_rejected_reason` when a matched entry signal is blocked by the
    # risk engine, but `_process` used to discard it entirely -- unlike a
    # manual `/orders` rejection, which at least gets a synchronous 403
    # with the reason, an autonomous entry the risk engine blocked left
    # zero record anywhere the user could ever see it happened.
    monkeypatch.setattr(
        RiskEngine, "evaluate", lambda self, proposal: RiskDecisionResult(RiskDecision.REJECT, [], "forced rejection for test")
    )

    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(SETUP)]

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"autotrade-rejected-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Auto Trade Rejected",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
            auto_trading_risk_per_trade_pct=1.0,
        )
        db.add(user)
        await db.flush()
        strategy = StrategyRow(
            user_id=user.id,
            name="Bullish FVG retest",
            definition={**STRATEGY_DEFINITION, "market": db_instrument.symbol},
            is_active=True,
            eligible_for_auto_trading=True,
        )
        db.add(strategy)
        await db.commit()
        user_id = user.id

    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        for i in range(len(candles)):
            async with async_session_factory() as db:
                await upsert_candles(db, db_instrument.id, "15m", [candles[i]])
            await supervisor.run_once()

        async with async_session_factory() as db:
            notifications = (
                await db.execute(select(Notification).where(Notification.user_id == user_id))
            ).scalars().all()
            assert len(notifications) >= 1
            assert all(n.type == NotificationType.ORDER_REJECTED for n in notifications)

            audits = (
                await db.execute(select(AuditLog).where(AuditLog.user_id == user_id, AuditLog.action == "autotrade.order_rejected"))
            ).scalars().all()
            assert len(audits) >= 1
            assert all(a.details["reason"] == "forced rejection for test" for a in audits)

            trades = (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
            assert trades == []  # forced rejection means it never actually opened
    finally:
        await _cleanup(user_id)
