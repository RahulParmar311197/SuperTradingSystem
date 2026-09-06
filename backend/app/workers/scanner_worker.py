"""ScannerWorker (blueprint §28-29, §66): periodically evaluates every
active strategy against every active instrument, persists matches as
`Signal` rows, journals the raw SMC pattern detections behind them as
`Setup` rows (blueprint §9 -- independent of whether any strategy actually
matched), and publishes results on `/ws/scanner` and `/ws/signals`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.redis import channel_name, heartbeat, publish
from app.database.models.instruments import Instrument
from app.database.models.strategy import Direction, Setup, Signal
from app.database.models.strategy import Strategy as StrategyRow
from app.database.session import async_session_factory
from app.ict.engine import ICTConfig, ICTEngine
from app.market.repository import get_candles
from app.smc.engine import SMCConfig, SMCContext, SMCEngine
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
            strategy_rows = (await db.execute(select(StrategyRow).where(StrategyRow.is_active.is_(True)))).scalars().all()
            instruments = (await db.execute(select(Instrument).where(Instrument.active.is_(True)))).scalars().all()

            strategies: list[tuple[uuid.UUID, StrategyDefinition]] = []
            for strategy_row in strategy_rows:
                try:
                    strategies.append((strategy_row.id, StrategyDefinition.model_validate(strategy_row.definition)))
                except Exception:
                    logger.exception("Strategy %s has an invalid definition; skipping", strategy_row.id)

            for instrument in instruments:
                # `StrategyDefinition.timeframe` is a first-class field of the
                # strategy, and every other consumer of the DSL honours it --
                # BacktestEngine, PaperTradingEngine and AutoTradeSupervisor
                # all load candles for `strategy.timeframe` (the supervisor
                # keeps its own `self.timeframe` purely for logging). This
                # worker used to load one series for `self.timeframe` and
                # evaluate *every* active strategy against it, silently
                # substituting the scan timeframe for the one the user
                # declared: a 1h strategy was matched against 15m structure,
                # its entry/stop came off a 15m dealing range, and the
                # persisted Signal was labelled "15m". Contexts are built
                # lazily and cached per timeframe so the "analyze once per
                # instrument" saving survives for the common case where every
                # strategy shares the scan timeframe.
                contexts: dict[str, EvaluationContext | None] = {}

                async def _context_for(timeframe: str) -> EvaluationContext | None:
                    if timeframe in contexts:
                        return contexts[timeframe]
                    candles = await get_candles(db, instrument.id, timeframe)
                    if len(candles) < 3:
                        contexts[timeframe] = None
                        return None
                    smc_context = self.smc_engine.analyze(candles)
                    if timeframe == self.timeframe:
                        # `setups` journal raw structure detections for the
                        # timeframe actually being scanned, independent of any
                        # strategy -- so only the scan timeframe's read is
                        # persisted, not every timeframe a strategy asked for.
                        await self._persist_new_setups(db, instrument.id, timeframe, smc_context)
                    contexts[timeframe] = EvaluationContext(
                        symbol=instrument.symbol,
                        timeframe=timeframe,
                        timestamp=candles[-1].timestamp,
                        current_price=candles[-1].close,
                        smc=smc_context,
                        ict=self.ict_engine.analyze(candles),
                        current_index=len(candles) - 1,
                    )
                    return contexts[timeframe]

                # Always analyze the scan timeframe, even if no strategy uses
                # it, so `setups` keeps being journaled as before.
                await _context_for(self.timeframe)

                for strategy_id, strategy in strategies:
                    context = await _context_for(strategy.timeframe)
                    if context is None:
                        # No usable history on the strategy's own timeframe.
                        # Skip it rather than falling back to the scan
                        # timeframe -- a signal computed off the wrong series
                        # is worse than no signal.
                        logger.debug(
                            "No %s candles for %s; skipping strategy %s",
                            strategy.timeframe,
                            instrument.symbol,
                            strategy_id,
                        )
                        continue
                    result = await self._evaluate(db, strategy, strategy_id, instrument, context)
                    if result is not None:
                        results.append(result)

        await publish(channel_name("scanner"), {"results": results})
        return results

    async def _persist_new_setups(self, db, instrument_id: uuid.UUID, timeframe: str, smc: SMCContext) -> None:
        """Journal raw SMC pattern detections (blueprint §9 `setups`) --
        structure breaks, fair value gaps, order blocks -- independent of
        whether any strategy matched on them. Idempotent per scan pass:
        every historical candle gets re-analyzed on every pass, so this
        only inserts the (setup_type, detected_at) combinations not
        already persisted for this instrument/timeframe.
        """
        candidates: list[tuple[str, datetime, dict]] = []
        for event in smc.structure_events:
            candidates.append((
                f"structure_{event.event_type.value.lower()}",
                event.timestamp,
                {"direction": event.direction.value, "broken_price": event.broken_price, "break_price": event.break_price},
            ))
        for fvg in smc.fair_value_gaps:
            candidates.append((
                "fair_value_gap",
                fvg.created_at,
                {"direction": fvg.direction.value, "top": fvg.top, "bottom": fvg.bottom},
            ))
        for order_block in smc.order_blocks:
            candidates.append((
                "order_block",
                order_block.created_at,
                {"direction": order_block.direction.value, "top": order_block.top, "bottom": order_block.bottom, "strength": order_block.strength},
            ))

        if not candidates:
            return

        min_detected_at = min(detected_at for _, detected_at, _ in candidates)
        stmt = select(Setup.setup_type, Setup.detected_at).where(
            Setup.instrument_id == instrument_id, Setup.timeframe == timeframe, Setup.detected_at >= min_detected_at
        )
        existing = {(row.setup_type, row.detected_at) for row in (await db.execute(stmt)).all()}

        wrote_any = False
        for setup_type, detected_at, data in candidates:
            if (setup_type, detected_at) in existing:
                continue
            db.add(Setup(instrument_id=instrument_id, timeframe=timeframe, setup_type=setup_type, data=data, detected_at=detected_at))
            wrote_any = True

        if wrote_any:
            await db.commit()

    async def _evaluate(
        self, db, strategy: StrategyDefinition, strategy_id: uuid.UUID, instrument: Instrument, context: EvaluationContext
    ) -> dict | None:
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

            # Same dedup problem `_persist_new_setups` above already
            # solves for `setups`, left open here: this worker re-scans
            # the same closed candles on every pass (interval_seconds=60
            # vs a 15m timeframe means ~15 passes before the underlying
            # data even changes), and a condition like "an unmitigated FVG
            # exists" stays true for as long as that gap stays unfilled --
            # often many passes. Without this check, one genuine setup
            # wrote a fresh `Signal` row and re-published to /ws/signals
            # every single pass for as long as it remained valid, flooding
            # `GET /signals`'s 200-row window and spamming connected
            # clients with the same "signal" repeatedly. Only the zone
            # actually changing (a new entry/stop/target) is a genuinely
            # new signal worth recording again.
            latest_stmt = (
                select(Signal)
                .where(Signal.instrument_id == instrument.id, Signal.strategy_id == strategy_id)
                .order_by(Signal.generated_at.desc())
                .limit(1)
            )
            latest = (await db.execute(latest_stmt)).scalar_one_or_none()
            if (
                latest is not None
                and latest.direction == trade_direction
                and round(float(latest.entry), 6) == round(outcome.entry, 6)
                and round(float(latest.stop), 6) == round(outcome.stop, 6)
                and round(float(latest.target), 6) == round(outcome.target, 6)
            ):
                return result

            db.add(
                Signal(
                    instrument_id=instrument.id,
                    strategy_id=strategy_id,
                    timeframe=context.timeframe,
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
            await heartbeat("scanner")
            await asyncio.sleep(self.interval_seconds)
