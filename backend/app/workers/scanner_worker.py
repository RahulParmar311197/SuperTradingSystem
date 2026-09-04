"""ScannerWorker (blueprint §28-29, §66): periodically evaluates every
active strategy against every active instrument, persists matches as
`Signal` rows, and publishes results on `/ws/scanner` and `/ws/signals`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.redis import channel_name, publish
from app.database.models.instruments import Instrument
from app.database.models.strategy import Direction, Signal
from app.database.models.strategy import Strategy as StrategyRow
from app.database.session import async_session_factory
from app.ict.engine import ICTConfig, ICTEngine
from app.market.repository import get_candles
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import StrategyDefinition
from app.strategy.engine import StrategyEngine

logger = logging.getLogger("workers.scanner")

# StrategyEvaluationResult.direction is "bullish"/"bearish" (the SMC bias
# vocabulary); the signals table stores trading direction (LONG/SHORT).
_BIAS_TO_TRADE_DIRECTION = {"bullish": Direction.LONG, "bearish": Direction.SHORT}


class ScannerWorker:
    def __init__(self, timeframe: str = "15m", interval_seconds: float = 60.0) -> None:
        self.timeframe = timeframe
        self.interval_seconds = interval_seconds
        self.smc_engine = SMCEngine(SMCConfig())
        self.ict_engine = ICTEngine(ICTConfig())
        self.strategy_engine = StrategyEngine()

    async def run_once(self) -> list[dict]:
        results: list[dict] = []
        async with async_session_factory() as db:
            strategies = (await db.execute(select(StrategyRow).where(StrategyRow.is_active.is_(True)))).scalars().all()
            instruments = (await db.execute(select(Instrument).where(Instrument.active.is_(True)))).scalars().all()

            for strategy_row in strategies:
                try:
                    strategy = StrategyDefinition.model_validate(strategy_row.definition)
                except Exception:
                    logger.exception("Strategy %s has an invalid definition; skipping", strategy_row.id)
                    continue

                for instrument in instruments:
                    result = await self._evaluate(db, strategy, strategy_row.id, instrument, self.timeframe)
                    if result is not None:
                        results.append(result)

        await publish(channel_name("scanner"), {"results": results})
        return results

    async def _evaluate(self, db, strategy: StrategyDefinition, strategy_id, instrument: Instrument, timeframe: str) -> dict | None:
        candles = await get_candles(db, instrument.id, timeframe)
        if len(candles) < 3:
            return None

        context = EvaluationContext(
            symbol=instrument.symbol,
            timeframe=timeframe,
            timestamp=candles[-1].timestamp,
            current_price=candles[-1].close,
            smc=self.smc_engine.analyze(candles),
            ict=self.ict_engine.analyze(candles),
        )
        outcome = self.strategy_engine.evaluate(strategy, context)
        result = {
            "instrument_id": str(instrument.id),
            "symbol": instrument.symbol,
            "matched": outcome.matched,
            "score": outcome.score,
            "direction": outcome.direction,
        }

        if outcome.matched:
            trade_direction = _BIAS_TO_TRADE_DIRECTION.get(outcome.direction.lower())
            if trade_direction is None:
                logger.warning("Unrecognized strategy direction %r for instrument %s; skipping signal", outcome.direction, instrument.id)
                return result if outcome.matched else None

            db.add(
                Signal(
                    instrument_id=instrument.id,
                    strategy_id=strategy_id,
                    timeframe=timeframe,
                    direction=trade_direction,
                    entry=outcome.entry,
                    stop=outcome.stop,
                    target=outcome.target,
                    risk_reward=outcome.risk_reward,
                    score=outcome.score,
                    context={"satisfied": outcome.satisfied, "missing": outcome.missing},
                    generated_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
            await publish(channel_name("signals", str(instrument.id)), result)
            await publish(channel_name("signals"), result)

        return result if outcome.matched else None

    async def run(self) -> None:
        logger.info("ScannerWorker starting, interval=%ss timeframe=%s", self.interval_seconds, self.timeframe)
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Scanner pass failed")
            await asyncio.sleep(self.interval_seconds)
