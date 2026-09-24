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
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.audit import record_audit
from app.core.metrics import ORDER_COUNT, RISK_REJECTION_COUNT
from app.core.config import get_settings
from app.core.redis import account_halt_reason, acquire_trade_lock, heartbeat, release_trade_lock
from app.database.models.instruments import Instrument
from app.database.models.strategy import Direction
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import ExecutionMode
from app.database.models.trading import Position as PositionRow
from app.database.models.trading import Trade as TradeRow
from app.database.models.notifications import NotificationType
from app.database.models.risk import RiskDecision as RiskEventDecision
from app.database.models.risk import RiskEvent
from app.database.models.users import TradingPermission, User
from app.database.session import async_session_factory
from app.market.repository import get_candles
from app.market.timeframes import timeframe_to_minutes
from app.notifications.service import create_notification
from app.paper.engine import PaperTradingEngine, RiskWindow
from app.risk.limits import RiskLimits
from app.strategy.dsl import StrategyDefinition
from app.trading.persistence import (
    AUTO_TRADE_SOURCE,
    load_auto_trade_entries_since,
    load_auto_trade_realized_pnl_since,
    load_open_positions,
    persist_position,
    risk_window_starts,
)
from app.trading.position_manager import PositionManager

logger = logging.getLogger("workers.autotrade")

# The `positions.source_key` every position this loop opens is written
# under. Named so the unmanaged-position sweep below and the writers that
# stamp it cannot drift apart.
AUTO_SOURCE_KEY = "auto"

# How many of its own bars old a candle may be before this loop refuses to
# trade on it. REASONED, NOT CALIBRATED: a bar is expected within one
# interval of its close, and this supervisor polls every
# `interval_seconds` (60s by default), so one bar of slack covers the
# ordinary case. At three, at least two bars are missing outright -- the
# feed is not merely late, it has stopped.
#
# Deliberately NOT `RiskLimits.market_data_max_staleness_seconds` (10s).
# That number describes a *tick*, which is what `POST /orders` measures
# through Redis; a 15m bar is already 900 seconds old the instant it
# closes, so applying the tick limit here would refuse every candle this
# loop has ever seen.
MAX_CANDLE_AGE_IN_BARS = 3


def candle_age_seconds(candle, timeframe: str, now: datetime) -> float:
    """How late `candle` is, measured from when its bar closed.

    A bar stamped at its open is not available until `timeframe` later, so
    the age that means anything is the excess beyond that -- not the raw
    difference, which would call every freshly-closed 15m bar 900s stale.
    Floored at 0: a clock skew that puts the bar slightly in the future is
    not negative staleness.
    """
    elapsed = (now - candle.timestamp).total_seconds()
    return max(0.0, elapsed - timeframe_to_minutes(timeframe) * 60)




class AutoTradeSupervisor:
    def __init__(self, timeframe: str = "15m", interval_seconds: float = 60.0) -> None:
        self.timeframe = timeframe
        self.interval_seconds = interval_seconds
        self._engines: dict[tuple[str, str, str], PaperTradingEngine] = {}
        self._engine_strategy_versions: dict[tuple[str, str, str], int] = {}
        self._opened_at: dict[tuple[str, str, str], datetime] = {}
        self._last_candle_seen: dict[tuple[str, str, str], datetime] = {}
        # One PositionManager per *user* (not per engine) -- an engine
        # exists per (strategy, instrument) pair, but a user's
        # `max_open_positions`/exposure limits are account-wide across
        # every instrument and strategy they're auto-trading, mirroring
        # `_UserTradingStack` (app/api/orders.py), which does the same for
        # the manual/live path. See the comment on `PaperTradingEngine`'s
        # `position_manager` constructor argument for what breaks without
        # this shared instance.
        self._position_managers: dict[str, PositionManager] = {}
        # One RiskWindow per *user*, for exactly the same reason as the
        # PositionManager above: blueprint §57's max_trades_per_day,
        # daily/weekly loss limits and repeated-rejection breaker are
        # account-wide, but an engine exists per (strategy, instrument)
        # pair. Held on the engine, each of those counters was per-triple,
        # so a user trading N instruments across M strategies got N*M times
        # the cap they configured -- and the daily-loss halt only fired
        # once a *single* pair had lost the whole limit by itself.
        self._risk_windows: dict[str, RiskWindow] = {}
        # Positions already reported as unmanaged, so the error below is one
        # line per position rather than one per 60-second pass.
        self._reported_unmanaged: set[str] = set()

    async def run_once(self) -> list[dict]:
        results: list[dict] = []
        async with async_session_factory() as db:
            eligible_users = (
                await db.execute(select(User).where(User.auto_trading_enabled.is_(True)))
            ).scalars().all()

            # Every (user, instrument) whose open position this pass actually
            # fed a candle to. What is left over is reported below: an open
            # position nothing is managing is the one state this loop must
            # never reach silently.
            managed: set[tuple[str, str]] = set()

            for user in eligible_users:
                if TradingPermission.AUTO_TRADE.value not in user.trading_permissions:
                    continue

                # **A halt stops new entries. It must not stop exits.**
                #
                # This used to `continue`, which skipped the user entirely --
                # and the stop on any position already open then stopped
                # being evaluated, because that stop exists nowhere else.
                # `ensure_protective_stop` is called only from POST /orders,
                # so there is no broker-side order behind an auto-traded
                # position: `PaperTradingEngine._maybe_exit`, run on the
                # candles this loop feeds it, IS the stop.
                #
                # Measured on the stop-loss fixture in
                # tests/workers/test_auto_trade_worker.py, halting the
                # account after the entry filled and before the bar that
                # breaks the stop:
                #
                #     not halted -> 1 trade, no open position
                #     halted     -> 0 trades, position still open,
                #                   stop 99.70, on a bar whose low was 90
                #
                # The ruling this now follows is already made twice in this
                # codebase, in these words: reconciliation halts an account
                # precisely when its positions look wrong, which is "the
                # worst moment to forbid closing them". `POST /orders` and
                # `POST /options/execute` both exempt a reducing order from
                # the halt for exactly that reason; this path had the
                # exemption missing rather than declined.
                entries_allowed = await account_halt_reason(str(user.id)) is None

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
                        outcome = await self._process(
                            db, user, strategy_row, strategy, instrument,
                            entries_allowed=entries_allowed, managed=managed,
                        )
                        if outcome is not None:
                            results.append(outcome)

            await self._report_unmanaged_positions(db, managed)

        return results

    async def _report_unmanaged_positions(self, db, managed: set[tuple[str, str]]) -> None:
        """Says so when an open auto-traded position is no longer being
        managed by anything.

        The gates above are about taking risk ON, and the halt one now
        exempts exits for that reason. Three others do not, and each of
        them leaves a live position whose stop has quietly stopped being
        enforced:

          - `auto_trading_enabled` turned off
          - the AUTO_TRADE permission revoked
          - the strategy that opened it deactivated or made ineligible

        Whether the loop should keep honouring a stop it placed after the
        operator has switched the robot off is a real question with two
        defensible answers -- `POST /orders` requires the LIVE_TRADE
        permission for every order including a reducing one, which argues
        for stopping; a stop that silently stops existing argues for
        continuing -- and guessing it is not this round's to do. What is
        not in question is that it must not be **silent**.

        Once per position, not once per pass: this runs every 60 seconds,
        and an operator who has to filter the warning will not read it.
        """
        open_auto = (
            await db.execute(
                select(PositionRow).where(
                    PositionRow.is_open.is_(True),
                    PositionRow.source_key == AUTO_SOURCE_KEY,
                )
            )
        ).scalars().all()
        for position in open_auto:
            key = (str(position.user_id), str(position.instrument_id))
            if key in managed or str(position.id) in self._reported_unmanaged:
                continue
            self._reported_unmanaged.add(str(position.id))
            logger.error(
                "Open auto-traded position %s (user %s, instrument %s, stop %s) is no longer "
                "being managed by this loop: nothing is evaluating its stop or target, and "
                "there is no broker-side protective order behind it. Auto-trading may have "
                "been disabled, the AUTO_TRADE permission revoked, the strategy that opened "
                "it deactivated, or its candle feed gone stale. Close it through POST /orders, "
                "or restore whichever of those stopped.",
                position.id, position.user_id, position.instrument_id, position.stop,
            )
            try:
                await create_notification(
                    db,
                    user_id=position.user_id,
                    notification_type=NotificationType.RECONCILIATION_REQUIRED,
                    title="An open position is no longer being managed",
                    body=(
                        "The autonomous loop is no longer evaluating the stop or target on an "
                        "open position. Nothing else is: there is no broker-side protective "
                        "order behind an auto-traded position. Close it manually or re-enable "
                        "auto-trading."
                    ),
                    data={"position_id": str(position.id), "instrument_id": str(position.instrument_id)},
                )
                await db.commit()
            except Exception:
                # Never let reporting take the loop down -- the same
                # reasoning as the heartbeat guards in this module.
                logger.exception("Could not notify about unmanaged position %s", position.id)

    async def _process(
        self,
        db,
        user: User,
        strategy_row: StrategyRow,
        strategy: StrategyDefinition,
        instrument: Instrument,
        *,
        entries_allowed: bool = True,
        managed: set[tuple[str, str]] | None = None,
    ) -> dict | None:
        key = (str(user.id), str(strategy_row.id), str(instrument.id))
        engine = self._engines.get(key)
        if engine is None:
            position_manager = self._position_managers.get(str(user.id))
            if position_manager is None:
                # Same restart gap the manual stack had: this worker's
                # book is process memory, mirrored into `positions` and
                # never read back, so a worker restart started flat and
                # `max_open_positions` / `current_exposure` both saw zero
                # while the account's auto-traded positions sat open in
                # Postgres. Rebuilt once per user, before any candle is
                # fed to an engine sharing this manager.
                position_manager = PositionManager()
                position_manager.restore(
                    await load_open_positions(
                        db, user.id, ExecutionMode.PAPER, source_key=AUTO_SOURCE_KEY
                    )
                )
                self._position_managers[str(user.id)] = position_manager
            risk_window = self._risk_windows.get(str(user.id))
            if risk_window is None:
                # The other half of the restart gap the position manager
                # above already closes. That book is rebuilt from
                # Postgres; these counters were not, so every worker start
                # handed the account a fresh `max_trades_per_day`,
                # `max_daily_loss_pct` and `max_weekly_loss_pct`.
                #
                # Measured, one strategy on two instruments, cap of one
                # trade per day: instrument A opens one entry and the
                # window reads trades_today=1; a second supervisor -- what
                # a restart produces -- then opens a second entry on
                # instrument B, 2 positions against a cap of 1, with zero
                # rejections naming max_trades_per_day.
                #
                # This is the same defect the manual `/orders` stack had,
                # on the path nobody is watching, and the worker restarts
                # on every deploy and whenever its supervision loop
                # restarts it.
                #
                # `repeated_rejections` is deliberately NOT rebuilt: it
                # counts *consecutive* broker rejections, this path
                # journals no orders at all, and nothing durable records a
                # rejection as one. Inventing a number for it would be
                # worse than starting it at zero, which is what a fresh
                # process legitimately knows.
                now = datetime.now(timezone.utc)
                day_start, week_start = risk_window_starts(now)
                risk_window = RiskWindow(
                    trades_today=await load_auto_trade_entries_since(
                        db, user.id, since=day_start, source_key=AUTO_SOURCE_KEY
                    ),
                    daily_pnl=await load_auto_trade_realized_pnl_since(db, user.id, since=day_start),
                    weekly_pnl=await load_auto_trade_realized_pnl_since(db, user.id, since=week_start),
                    # Set so the rebuilt window states the day it
                    # covers rather than depending on something later to
                    # name it. Unlike the manual stack's equivalent, this
                    # is NOT load-bearing today and that was measured, not
                    # assumed: injecting these two lines away left all
                    # thirteen tests green, because `PaperTradingEngine`
                    # rolls this window with every candle and `roll` sets
                    # the marks itself when they are None. The manual
                    # stack differs only because `GET /positions` builds
                    # it without ever rolling it. Kept because a window
                    # whose counters cover today should say so on its own
                    # terms -- the alternative depends on a call made
                    # somewhere else.
                    risk_day=now.date(),
                    risk_week=now.isocalendar()[:2],
                )
                self._risk_windows[str(user.id)] = risk_window
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
                position_manager=position_manager,
                strategy_id=str(strategy_row.id),
                risk_window=risk_window,
                source_key=AUTO_SOURCE_KEY,
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
        previously_seen = self._last_candle_seen.get(key)
        self._last_candle_seen[key] = latest.timestamp

        def defer_this_candle() -> None:
            """Undo the mark above so the next pass retries this candle.

            Only the paths that decline to PROCESS the candle at all may
            call this. Without it, deferring on a busy account did not cost
            one candle -- it dropped the entry permanently, because the
            guard above would refuse to look at that timestamp again.
            Measured: the account's lock held by another process, the
            worker skipped the entry bar, and the next pass opened nothing.
            """
            if previously_seen is None:
                self._last_candle_seen.pop(key, None)
            else:
                self._last_candle_seen[key] = previously_seen

        # This loop trades unattended, and until now it traded whatever the
        # newest *stored* candle was, however old that was.
        #
        # `PaperTradingEngine` builds its `TradeRiskProposal` with
        # `market_data_age_seconds=0.0`, so the `market_data_fresh` check
        # could not fail here -- and the `RiskEvent` row recorded it as a
        # check that had passed. Measured by driving the engine with a bar
        # from January while the clock said September: proposal age 0.0,
        # `market_data_fresh: True`. `POST /orders` computes a real age
        # from Redis and enforces a limit; this path had the gate in name
        # only. It is the same shape as the `strategy_allocation=0.0` that
        # a previous round had to fix on this very proposal.
        #
        # The guard belongs here rather than in the engine: this is the
        # only unattended caller, and the other one
        # (`POST /paper/{id}/candle`) is an operator deliberately handing
        # over a bar, where freshness is not a property of anything.
        #
        # Nothing about this is hypothetical. `_last_candle_seen` is
        # in-memory, so a worker restart clears it and the very next pass
        # acts on the newest stored bar whatever its date -- and candles
        # only reach the store when someone runs `POST /admin/backfill`.
        age = candle_age_seconds(latest, strategy.timeframe, datetime.now(timezone.utc))
        max_age = timeframe_to_minutes(strategy.timeframe) * 60 * MAX_CANDLE_AGE_IN_BARS
        if age > max_age:
            logger.warning(
                "Not auto-trading %s on %s: newest stored candle closed %.0fs late (limit %.0fs, "
                "%d bars). The feed has stopped or history was never backfilled -- see "
                "POST /admin/backfill.",
                instrument.symbol,
                strategy.timeframe,
                age,
                max_age,
                MAX_CANDLE_AGE_IN_BARS,
            )
            return None

        # Seed a freshly-built engine with the stored history behind
        # `latest`, so its first evaluation analyses the same series every
        # other component analyses.
        #
        # `PaperTradingEngine` keeps its own `self.candles`, starts it
        # empty, appends one bar per `on_candle`, and runs
        # `smc_engine.analyze(self.candles)` over *that* list. This
        # supervisor already loads the whole stored series a few lines
        # above and then passed only `candles[-1]`, so the analysis window
        # was not the instrument's history -- it was however long this
        # process had been running. Every sibling passes the full series:
        # `ScannerWorker`, `POST /scanner`, `GET /charts/{id}/smc`,
        # `POST /backtest`, `POST /replay`, `POST /ai/analyze`.
        #
        # That is not a warm-up that clears in three bars.
        # `detect_session_levels` only emits a PREVIOUS_DAY_* /
        # PREVIOUS_WEEK_* pool when the candle list it is given spans a
        # bucket boundary, so an engine built mid-session sees zero
        # previous-day pools -- and therefore zero sweeps -- for the rest
        # of that session, and up to a week for the weekly levels.
        # `ConditionType.LIQUIDITY_SWEEP` reads exactly those pools, and
        # it is the blueprint's own canonical strategy. Measured on three
        # days of 15m bars: the scanner reported `matched=True` on the
        # same instrument and bar where this supervisor reported no
        # signal, with `engine.candles` holding 1 bar against 75 in the
        # database.
        #
        # It also moves numbers rather than only decisions:
        # `smc.dealing_range` is the stop for the default
        # `entry.type="market"`, so entry, stop and the
        # `calculate_position_size` quantity all came off a window whose
        # length was process uptime.
        #
        # Seeded after the `_last_candle_seen` guard, not before: an early
        # return there would leave the engine holding `candles[:-1]`
        # having never consumed `latest`, and the next pass would append a
        # newer bar over that gap. `[:-1]` because `on_candle` appends
        # `latest` itself. A new engine is built on a worker restart, but
        # also whenever a strategy is first marked
        # `eligible_for_auto_trading` or an instrument first becomes
        # active -- i.e. on ordinary onboarding, against instruments that
        # already have months of stored candles.
        if not engine.candles:
            engine.candles = list(candles[:-1])

        position_before = engine.position_manager.get(engine.account_id, engine.symbol)
        snapshot = None
        # The strategy that actually opened this position, stamped by
        # `PaperTradingEngine` when the entry filled. `PositionManager` is
        # shared per *user* and keyed only by (account_id, symbol), while an
        # engine exists per (user, strategy, instrument) -- so whichever
        # strategy is iterated first reaches a shared position and runs
        # `_maybe_exit` on it, even when a different strategy opened it.
        # Without this, the closing engine journaled the trade against
        # *itself*: wrong `strategy_id`, wrong `strategy_version`, wrong
        # journal/notification name, and `opened_at` falling back to the
        # closing candle because `_opened_at` is keyed by the opener's
        # triple. `None` means the position predates this attribution (or
        # came from a path that doesn't stamp it), in which case the
        # observing engine is still the best answer available.
        owner_strategy_id = None
        if position_before is not None and position_before.is_open:
            owner_strategy_id = position_before.strategy_id
            snapshot = {
                "direction": Direction.LONG if position_before.is_long else Direction.SHORT,
                "quantity": abs(position_before.quantity),
                "entry_price": position_before.average_price,
                "stop": position_before.stop,
                "target": position_before.target,
            }

        if not entries_allowed and not (position_before is not None and position_before.is_open):
            # Halted, and nothing open on this instrument. `on_candle`
            # evaluates an entry whenever no position is open, so feeding
            # it here would open one straight through the halt. With a
            # position open it cannot: `on_candle` returns immediately
            # after `_maybe_exit`, which is exactly the exit-only pass the
            # halt must not block.
            return None

        # Marked here, not where the open position is read above: the pass
        # that *opens* a position sees no position beforehand, so recording
        # it there reported every fresh entry as unmanaged on its very own
        # pass -- measured, one spurious notification per trade, which is
        # how a real warning becomes noise. What makes a position managed is
        # that a candle reached its engine, which is exactly here.
        if managed is not None:
            managed.add((str(user.id), str(instrument.id)))

        # The other half of the cross-process race that
        # `app/api/orders.py::serialize_user_trading` guards. This
        # supervisor runs as its own docker-compose service, so an
        # `asyncio.Lock` in the API process cannot reach it: both read the
        # account's exposure, neither sees the other's in-flight fill, and
        # both approve. Measured with this side held provably mid-fill --
        # past its risk gate, position not yet in Postgres -- a manual
        # order that the same gate refuses when run sequentially was
        # accepted, taking the account to 111.2% of a 100% limit.
        #
        # Unattended, so there is no 503 to return: failing closed means
        # skipping this entry and taking it on the next pass, which costs
        # one candle rather than a breached limit.
        try:
            lock_token = await acquire_trade_lock(str(user.id))
        except Exception:
            logger.exception("Could not reach Redis for the trade lock on account %s", user.id)
            if not get_settings().trade_lock_fail_open:
                defer_this_candle()
                return
            lock_token = None
        else:
            if lock_token is None:
                logger.info(
                    "Account %s is busy in another process; deferring this candle", user.id
                )
                defer_this_candle()
                return
        try:
            outcome = await engine.on_candle(latest, db)

            # Blueprint §9/§86: mirrors app/api/paper.py's feed_candle fix --
            # this is the same PaperTradingEngine driving blueprint §54's
            # flagship autonomous trading loop, and it never persisted a
            # `positions` row either. Every position this supervisor ever
            # opened was invisible to GET /portfolio, GET /admin/
            # portfolio-snapshot, and the correlated-exposure risk check for
            # its entire open lifetime -- those only ever saw it once it
            # closed and a Trade row appeared, understating a user's real
            # (simulated) exposure by however much autonomous trading itself
            # was holding, for as long as it stayed open.
            position_after = engine.position_manager.get(engine.account_id, engine.symbol)
            if position_after is not None:
                await persist_position(
                    db, user.id, instrument.id, position_after, execution_mode=ExecutionMode.PAPER, source_key=AUTO_SOURCE_KEY
                )

            if outcome.risk_checks is not None:
                # Same audit gap and fix as app/api/paper.py's feed_candle -- this
                # supervisor drives the identical PaperTradingEngine/RiskEngine
                # unattended, 24/7, with no synchronous caller to see a rejection;
                # without this, `GET /admin/risk-events` never saw a single
                # autonomous-trading decision, approved or rejected.
                db.add(
                    RiskEvent(
                        user_id=user.id,
                        decision=RiskEventDecision.REJECT if outcome.risk_rejected_reason is not None else RiskEventDecision.APPROVE,
                        reason=outcome.risk_rejected_reason,
                        checks=outcome.risk_checks,
                    )
                )
                await db.commit()
                if outcome.risk_rejected_reason is not None:
                    # `risk_rejections_total` is the metric an operator alerts
                    # on to learn an account has stopped trading. Only
                    # app/api/orders.py incremented it, so THIS path -- the
                    # unattended one, running 24/7 with nobody watching -- was
                    # the one invisible to monitoring. Measured with a cap of
                    # one trade a day and two instruments: the supervisor
                    # opened a position and had a second entry refused by the
                    # risk engine, writing RiskEvent rows for both, and the
                    # counter stayed at 0.0 throughout.
                    RISK_REJECTION_COUNT.inc()

            if outcome.order_created:
                self._opened_at[key] = latest.timestamp
                # Same gap on the fill side: `orders_total` counted manual
                # (app/api/orders.py) and options (app/api/options.py, round
                # 162) orders and no autonomous one. `order_status` is the
                # status this order actually reached at the broker, which the
                # engine computes and used to throw away -- labelling it with
                # anything else would put autonomous fills in a bucket the
                # other two paths never use.
                if outcome.order_status is not None:
                    ORDER_COUNT.labels(outcome.order_status.value).inc()
                direction = outcome.signal.direction if outcome.signal else None
                await record_audit(
                    db,
                    actor="system",
                    action="autotrade.order_placed",
                    user_id=user.id,
                    details={"strategy": strategy_row.name, "symbol": instrument.symbol, "direction": direction},
                )
                # Blueprint §63 mandates a "Trade executed" notification -- the
                # sibling risk_rejected_reason/closed_position_pnl branches
                # below both notify, but this one, the actual open of a
                # position, never did. This path has no synchronous HTTP
                # response for anyone to see the way manual POST /orders does,
                # so without this an opened autonomous trade was as invisible
                # as a rejected one used to be.
                await create_notification(
                    db,
                    user_id=user.id,
                    notification_type=NotificationType.TRADE_EXECUTED,
                    title=f"{instrument.symbol} auto-trade executed",
                    body=f"Opened {direction or 'a'} position in {instrument.symbol}",
                    data={"strategy": strategy_row.name, "symbol": instrument.symbol, "direction": direction},
                )

            if outcome.risk_rejected_reason is not None:
                # Blueprint §63 mandates an "Order rejected" notification. This
                # path has no synchronous HTTP response the way manual
                # POST /orders does (that endpoint at least returns a 403 with
                # the reason) -- without this, an autonomous entry the risk
                # engine blocked left absolutely no record anywhere the user
                # could ever see it happened.
                await record_audit(
                    db,
                    actor="system",
                    action="autotrade.order_rejected",
                    user_id=user.id,
                    details={"strategy": strategy_row.name, "symbol": instrument.symbol, "reason": outcome.risk_rejected_reason},
                )
                # Blueprint §63 lists "Daily loss limit" as its own
                # notification event, distinct from a generic order rejection
                # -- see the identical comment in app/api/paper.py's
                # feed_candle.
                rejection_notification_type = (
                    NotificationType.DAILY_LOSS_LIMIT
                    if outcome.risk_failed_check == "daily_loss_limit"
                    else NotificationType.ORDER_REJECTED
                )
                await create_notification(
                    db,
                    user_id=user.id,
                    notification_type=rejection_notification_type,
                    title=f"{instrument.symbol} auto-trade rejected",
                    body=outcome.risk_rejected_reason,
                    data={"strategy": strategy_row.name, "symbol": instrument.symbol, "reason": outcome.risk_rejected_reason},
                )

            if outcome.closed_position_pnl is not None and snapshot is not None:
                # Journal against whoever opened the position, not whoever
                # happened to observe the close -- see `owner_strategy_id`
                # above. The owner's row carries the `version` that was live
                # when the entry was taken, which is what blueprint §91 means
                # by "always know exactly which version created a trade".
                owner_row = strategy_row
                owner_key = key
                if owner_strategy_id is not None and owner_strategy_id != str(strategy_row.id):
                    owner_row = await db.get(StrategyRow, uuid.UUID(owner_strategy_id)) or strategy_row
                    owner_key = (str(user.id), owner_strategy_id, str(instrument.id))
                # The opener stamped `_opened_at` under its own triple, so pop
                # the owner's key -- popping this engine's would miss and fall
                # back to the closing candle, collapsing the holding period to
                # zero.
                opened_at = self._opened_at.pop(owner_key, latest.timestamp)
                risk_per_unit = abs(snapshot["entry_price"] - snapshot["stop"]) if snapshot["stop"] else None
                r_multiple = (
                    (outcome.closed_position_pnl / snapshot["quantity"]) / risk_per_unit if risk_per_unit else None
                )
                db.add(
                    TradeRow(
                        user_id=user.id,
                        instrument_id=instrument.id,
                        strategy_id=owner_row.id,
                        strategy_version=owner_row.version,
                        execution_mode=ExecutionMode.PAPER,
                        direction=snapshot["direction"],
                        entry_price=snapshot["entry_price"],
                        exit_price=outcome.exit_price,
                        quantity=snapshot["quantity"],
                        stop=snapshot["stop"],
                        target=snapshot["target"],
                        pnl=outcome.closed_position_pnl,
                        r_multiple=r_multiple,
                        opened_at=opened_at,
                        closed_at=latest.timestamp,
                        journal={
                            # Names this writer, the way `manual_order` and
                            # `manual_paper` already name theirs. Nothing had
                            # to tell the three apart until the risk counters
                            # started being rebuilt from this journal.
                            "source": AUTO_TRADE_SOURCE,
                            "strategy": owner_row.name,
                            "symbol": instrument.symbol,
                            "timeframe": strategy.timeframe,
                        },
                    )
                )
                await db.commit()
                # Blueprint §63 lists SL/TP hits as their own notification
                # events, distinct from a generic "position closed" -- see the
                # identical comment in app/api/paper.py's feed_candle.
                notification_type = {
                    "stop_loss": NotificationType.SL_HIT,
                    "take_profit": NotificationType.TP_HIT,
                }.get(outcome.exit_reason, NotificationType.POSITION_CLOSED)
                await create_notification(
                    db,
                    user_id=user.id,
                    notification_type=notification_type,
                    title=f"{instrument.symbol} auto-trade closed",
                    body=f"Realized P&L: {outcome.closed_position_pnl:.2f}",
                    data={"strategy": owner_row.name, "pnl": outcome.closed_position_pnl, "exit_reason": outcome.exit_reason},
                )

            return {
                "user_id": str(user.id),
                "strategy_id": str(strategy_row.id),
                "instrument_id": str(instrument.id),
                "order_created": outcome.order_created,
                "closed_pnl": outcome.closed_position_pnl,
            }
        finally:
            # Released only once every write this candle produced has
            # committed. An earlier version released it directly after
            # `on_candle`, which left the position row -- the very state
            # the other process's exposure gate reads -- still uncommitted
            # for the width of `persist_position` below. Measured with the
            # gate held in exactly that gap: the same manual order came
            # back 201 and the account reached 111.21%, i.e. the fix did
            # not hold at all. Covering the whole tail, rather than just
            # that one call, is deliberate: a later edit that journals
            # something else a risk gate reads cannot fall outside it.
            if lock_token is not None:
                await release_trade_lock(str(user.id), lock_token)

    async def run(self) -> None:
        logger.info("AutoTradeSupervisor starting, interval=%ss timeframe=%s", self.interval_seconds, self.timeframe)
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Auto-trade pass failed")
            # See ScannerWorker.run: `heartbeat` is a bare `redis.set`, and
            # outside this guard one transient Redis error ended the task
            # after a single pass -- silently stopping unattended
            # autonomous trading (§54) while the process stayed up and
            # looked healthy.
            try:
                await heartbeat("auto_trade")
            except Exception:
                logger.exception("Auto-trade heartbeat failed (Redis unreachable?) — the loop continues")
            await asyncio.sleep(self.interval_seconds)
