"""AutoTradeSupervisor (blueprint §54, §128 Stage 10 "Autonomous trading").

`ScannerWorker` already runs WATCH/SCAN/DETECT (it evaluates every active
strategy against every instrument and persists a `Signal` row on a match).
This supervisor is what turns a match into VALIDATE/RISK CHECK/TRADE/
MONITOR/EXIT/JOURNAL — but only for a (user, strategy) pair that has
explicitly opted in. Every one of these must hold before a single order is
placed:

  - `user.auto_trading_enabled` is True — set only via POST
    /auto-trading/enable with `confirm: true` (blueprint §102), never a
    default.
  - `TradingPermission.AUTO_TRADE` is in the user's permissions.
  - the strategy is `is_active` AND `eligible_for_auto_trading`.
  - the account isn't halted (`app.core.redis.account_halt_reason`) —
    same flag `ReconciliationWorker` raises.

This drives `PaperTradingEngine` (i.e. `MockBroker`) for every account
right now — live or not — because there is no authenticated live broker
adapter tied to a real account yet (see app/brokers/upstox,
app/brokers/dhan). Wiring a real account's broker in here instead of
`MockBroker` is the last step before this is actually live autonomous
trading; until then this is autonomous *paper* trading.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.core.audit import record_audit
from app.core.redis import account_halt_reason, heartbeat
from app.database.models.instruments import Instrument
from app.database.models.strategy import Direction
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import ExecutionMode
from app.database.models.trading import Trade as TradeRow
from app.database.models.notifications import NotificationType
from app.database.models.users import TradingPermission, User
from app.database.session import async_session_factory
from app.market.repository import get_candles
from app.notifications.service import create_notification
from app.paper.engine import PaperTradingEngine
from app.risk.limits import RiskLimits
from app.strategy.dsl import StrategyDefinition

logger = logging.getLogger("workers.autotrade")


class AutoTradeSupervisor:
    def __init__(self, timeframe: str = "15m", interval_seconds: float = 60.0) -> None:
        self.timeframe = timeframe
        self.interval_seconds = interval_seconds
        self._engines: dict[tuple[str, str, str], PaperTradingEngine] = {}
        self._engine_strategy_versions: dict[tuple[str, str, str], int] = {}
        self._opened_at: dict[tuple[str, str, str], datetime] = {}
        self._last_candle_seen: dict[tuple[str, str, str], datetime] = {}

    async def run_once(self) -> list[dict]:
        results: list[dict] = []
        async with async_session_factory() as db:
            eligible_users = (
                await db.execute(select(User).where(User.auto_trading_enabled.is_(True)))
            ).scalars().all()

            for user in eligible_users:
                if TradingPermission.AUTO_TRADE.value not in user.trading_permissions:
                    continue
                if await account_halt_reason(str(user.id)) is not None:
                    continue

                strategy_rows = (
                    await db.execute(
                        select(StrategyRow).where(
                            StrategyRow.user_id == user.id,
                            StrategyRow.is_active.is_(True),
                            StrategyRow.eligible_for_auto_trading.is_(True),
                        )
                    )
                ).scalars().all()
                if not strategy_rows:
                    continue

                instruments = (await db.execute(select(Instrument).where(Instrument.active.is_(True)))).scalars().all()

                for strategy_row in strategy_rows:
                    try:
                        strategy = StrategyDefinition.model_validate(strategy_row.definition)
                    except Exception:
                        logger.exception("Strategy %s has an invalid definition; skipping", strategy_row.id)
                        continue

                    for instrument in instruments:
                        outcome = await self._process(db, user, strategy_row, strategy, instrument)
                        if outcome is not None:
                            results.append(outcome)

        return results

    async def _process(self, db, user: User, strategy_row: StrategyRow, strategy: StrategyDefinition, instrument: Instrument) -> dict | None:
        key = (str(user.id), str(strategy_row.id), str(instrument.id))
        engine = self._engines.get(key)
        if engine is None:
            engine = PaperTradingEngine(
                strategy,
                symbol=instrument.symbol,
                account_id=str(user.id),
                risk_limits=RiskLimits(
                    risk_per_trade_pct=float(user.auto_trading_risk_per_trade_pct),
                    max_daily_loss_pct=float(user.auto_trading_daily_loss_limit_pct),
                    max_trades_per_day=user.auto_trading_max_trades_per_day,
                    max_open_positions=user.auto_trading_max_positions,
                ),
            )
            self._engines[key] = engine
            self._engine_strategy_versions[key] = strategy_row.version
        elif self._engine_strategy_versions.get(key) != strategy_row.version:
            # The user edited this strategy (PUT /strategies/{id} bumps
            # `version` and rewrites `definition`) since this engine was
            # built. `PaperTradingEngine.strategy` is only ever read, never
            # reassigned internally (see app/paper/engine.py), so without
            # this the engine would keep evaluating every future candle
            # against the *old* DSL indefinitely -- while the `Trade` row
            # journaled below still stamped `strategy_row.version` (the
            # *current* version), making the audit trail actively wrong,
            # not just stale. Only swap the strategy definition in place;
            # rebuilding the whole engine would also reset its `MockBroker`
            # balance and discard any currently open position.
            engine.strategy = strategy
            self._engine_strategy_versions[key] = strategy_row.version

        # Risk settings (`POST /auto-trading/enable`) aren't versioned like
        # a strategy definition -- refresh them every pass so a change
        # takes effect on this engine's very next candle instead of never.
        engine.risk_engine.limits = RiskLimits(
            risk_per_trade_pct=float(user.auto_trading_risk_per_trade_pct),
            max_daily_loss_pct=float(user.auto_trading_daily_loss_limit_pct),
            max_trades_per_day=user.auto_trading_max_trades_per_day,
            max_open_positions=user.auto_trading_max_positions,
        )

        candles = await get_candles(db, instrument.id, strategy.timeframe)
        if not candles:
            return None
        latest = candles[-1]
        if self._last_candle_seen.get(key) == latest.timestamp:
            return None
        self._last_candle_seen[key] = latest.timestamp

        position_before = engine.position_manager.get(engine.account_id, engine.symbol)
        snapshot = None
        if position_before is not None and position_before.is_open:
            snapshot = {
                "direction": Direction.LONG if position_before.is_long else Direction.SHORT,
                "quantity": abs(position_before.quantity),
                "entry_price": position_before.average_price,
                "stop": position_before.stop,
                "target": position_before.target,
            }

        outcome = await engine.on_candle(latest)

        if outcome.order_created:
            self._opened_at[key] = latest.timestamp
            await record_audit(
                db,
                actor="system",
                action="autotrade.order_placed",
                user_id=user.id,
                details={"strategy": strategy_row.name, "symbol": instrument.symbol, "direction": outcome.signal.direction if outcome.signal else None},
            )

        if outcome.closed_position_pnl is not None and snapshot is not None:
            opened_at = self._opened_at.pop(key, latest.timestamp)
            risk_per_unit = abs(snapshot["entry_price"] - snapshot["stop"]) if snapshot["stop"] else None
            r_multiple = (
                (outcome.closed_position_pnl / snapshot["quantity"]) / risk_per_unit if risk_per_unit else None
            )
            db.add(
                TradeRow(
                    user_id=user.id,
                    instrument_id=instrument.id,
                    strategy_id=strategy_row.id,
                    strategy_version=strategy_row.version,
                    execution_mode=ExecutionMode.PAPER,
                    direction=snapshot["direction"],
                    entry_price=snapshot["entry_price"],
                    exit_price=latest.close,
                    quantity=snapshot["quantity"],
                    stop=snapshot["stop"],
                    target=snapshot["target"],
                    pnl=outcome.closed_position_pnl,
                    r_multiple=r_multiple,
                    opened_at=opened_at,
                    closed_at=latest.timestamp,
                    journal={
                        "strategy": strategy_row.name,
                        "symbol": instrument.symbol,
                        "timeframe": strategy.timeframe,
                    },
                )
            )
            await db.commit()
            await create_notification(
                db,
                user_id=user.id,
                notification_type=NotificationType.POSITION_CLOSED,
                title=f"{instrument.symbol} auto-trade closed",
                body=f"Realized P&L: {outcome.closed_position_pnl:.2f}",
                data={"strategy": strategy_row.name, "pnl": outcome.closed_position_pnl},
            )

        return {
            "user_id": str(user.id),
            "strategy_id": str(strategy_row.id),
            "instrument_id": str(instrument.id),
            "order_created": outcome.order_created,
            "closed_pnl": outcome.closed_position_pnl,
        }

    async def run(self) -> None:
        logger.info("AutoTradeSupervisor starting, interval=%ss timeframe=%s", self.interval_seconds, self.timeframe)
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Auto-trade pass failed")
            await heartbeat("auto_trade")
            await asyncio.sleep(self.interval_seconds)
