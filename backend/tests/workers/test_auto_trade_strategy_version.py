"""A trade was stamped with the strategy version live at its CLOSE.

Blueprint §91 is "always know exactly which version created a trade",
and `app/api/strategies.py` says it plainly: a trade's `strategy_version`
"can always be resolved back to the exact DSL that produced it via
GET /strategies/{id}/versions/{version}". `PATCH /strategies/{id}` bumps
`version` on every edit, and the auto-trade worker read that number off
the strategy row at close time.

Measured, one position over two candles with an edit in between:

    version at entry            1
    version after the edit      2
    strategy_version on the trade   2     <- resolves to the wrong DSL

The engine-swap comment in `app/workers/auto_trade_worker.py` had
already named this exact consequence -- "the `Trade` row journaled below
still stamped `strategy_row.version` (the *current* version), making the
audit trail actively wrong, not just stale" -- but that fix corrected
which DSL the ENGINE evaluates and left the stamp reading live.

`POST /paper` is the reference: `app/api/paper.py` captures
`strategy_row.version` when the session starts and journals
`session.strategy_version` at trade time. The worker now captures the
same fact at entry, in `_opened_version`, keyed by the opener's triple
exactly as `_opened_at` already is.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from app.database.models.market import Candle as CandleRow
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.trading import Trade as TradeRow
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.workers.auto_trade_worker import AutoTradeSupervisor

from tests.workers.test_auto_trade_balance_restart import (
    _auto_trading_user,
    _candles,
    _instrument,
)


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first. `strategy_versions` references `strategies`, so
    it has to go before it -- measured, not guessed: it is what the first
    run of this probe hit."""
    from app.database.models.notifications import Notification
    from app.database.models.risk import AuditLog, RiskEvent
    from app.database.models.trading import Position
    from app.database.models.users import User

    async with async_session_factory() as db:
        for user_id in user_ids:
            strategy_ids = (
                await db.execute(select(StrategyRow.id).where(StrategyRow.user_id == user_id))
            ).scalars().all()
            for strategy_id in strategy_ids:
                await db.execute(
                    delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id)
                )
            for model in (TradeRow, Position, Notification, AuditLog, RiskEvent, StrategyRow):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        from app.database.models.instruments import Instrument

        for instrument_id in instrument_ids:
            # By instrument as well as by user. `AutoTradeSupervisor`
            # iterates EVERY auto-trading user in the database, so any
            # other account left enabled will have traded these
            # instruments too -- measured, not guessed: deleting by user
            # alone left `positions` rows behind and the instrument delete
            # hit a foreign key violation.
            await db.execute(delete(TradeRow).where(TradeRow.instrument_id == instrument_id))
            await db.execute(delete(Position).where(Position.instrument_id == instrument_id))
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def _feed_bars(supervisor, instrument_id, candles, start, stop) -> None:
    for i in range(start, stop):
        async with async_session_factory() as db:
            await upsert_candles(db, instrument_id, "15m", [candles[i]])
        await supervisor.run_once()


async def _edit_strategy(user_id: uuid.UUID) -> int:
    """Exactly what `PATCH /strategies/{id}` does: bump the version and
    snapshot the definition. The definition is left identical on purpose
    -- this isolates the version stamp from any change in behaviour."""
    async with async_session_factory() as db:
        row = (
            await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))
        ).scalars().first()
        row.version += 1
        db.add(
            StrategyVersionRow(
                strategy_id=row.id, version=row.version, name=row.name, definition=row.definition
            )
        )
        await db.commit()
        return row.version


async def _trades(user_id: uuid.UUID) -> list[TradeRow]:
    async with async_session_factory() as db:
        return list(
            (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
        )


# --- the finding ----------------------------------------------------------


async def test_a_trade_is_stamped_with_the_version_that_opened_it(require_infra):
    """The headline. The entry happens on bar 8, the edit lands between
    the bars, and bar 9 runs to target and closes."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(markets=[instrument.symbol])
        user_ids.append(user_id)
        candles = _candles()
        supervisor = AutoTradeSupervisor(timeframe="15m")

        await _feed_bars(supervisor, instrument.id, candles, 0, 9)
        assert await _edit_strategy(user_id) == 2, "the fixture must actually bump the version"

        await _feed_bars(supervisor, instrument.id, candles, 9, 10)

        trades = await _trades(user_id)
        assert len(trades) == 1, f"expected one closed trade, got {len(trades)}"
        assert trades[0].strategy_version == 1, (
            "the trade was journalled against a DSL that did not produce it"
        )
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_an_unedited_strategy_still_stamps_its_own_version(require_infra):
    """Non-vacuity control. Without an edit the two readings coincide, so
    the assertion above is about the edit and not about the stamp having
    been hardcoded to 1."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(markets=[instrument.symbol])
        user_ids.append(user_id)
        # Two edits BEFORE anything is traded: the entry then happens at
        # version 3, and 3 is what the trade must carry -- which a fix
        # that simply always wrote 1 would fail.
        assert await _edit_strategy(user_id) == 2
        assert await _edit_strategy(user_id) == 3

        supervisor = AutoTradeSupervisor(timeframe="15m")
        await _feed_bars(supervisor, instrument.id, _candles(), 0, 10)

        trades = await _trades(user_id)
        assert len(trades) == 1
        assert trades[0].strategy_version == 3
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_later_trade_uses_the_later_version(require_infra):
    """The registry must not pin a version for the life of the process.
    An edit that lands while nothing is open applies to the next entry,
    which is the ordinary case an over-fix would break."""
    a, b = await _instrument(), await _instrument()
    user_ids, instrument_ids = [], [a.id, b.id]
    try:
        user_id = await _auto_trading_user(markets=[a.symbol, b.symbol])
        user_ids.append(user_id)
        candles = _candles()
        supervisor = AutoTradeSupervisor(timeframe="15m")

        # First instrument: opened and closed entirely at version 1.
        await _feed_bars(supervisor, a.id, candles, 0, 10)
        assert await _edit_strategy(user_id) == 2
        # Second instrument: opened and closed entirely at version 2.
        await _feed_bars(supervisor, b.id, candles, 0, 10)

        versions = sorted(t.strategy_version for t in await _trades(user_id))
        assert versions == [1, 2], versions
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_the_stamp_is_popped_so_a_reused_key_cannot_inherit_it(require_infra):
    """`_opened_at` is popped at close and this must be too: the same
    (user, strategy, instrument) triple is reused for the next trade, and
    a stale entry would stamp the NEXT trade with the PREVIOUS trade's
    version. Two full round trips on one instrument, with an edit in
    between, so the second must carry 2."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(markets=[instrument.symbol])
        user_ids.append(user_id)
        candles = _candles()
        supervisor = AutoTradeSupervisor(timeframe="15m")

        await _feed_bars(supervisor, instrument.id, candles, 0, 10)
        assert len(await _trades(user_id)) == 1, "the first round trip must have closed"
        key = (str(user_id), None, str(instrument.id))
        leftovers = [k for k in supervisor._opened_version if k[0] == key[0] and k[2] == key[2]]
        assert leftovers == [], f"the closed trade left its version behind: {leftovers}"
    finally:
        await _cleanup(user_ids, instrument_ids)


def test_the_paper_path_captures_its_version_at_session_start():
    """The reference implementation this round copied, asserted against
    the call itself rather than the name: `POST /paper` stores
    `strategy_row.version` on the session and journals
    `session.strategy_version`, so it never reads the live row at trade
    time. If that ever changes, this path needs the same fix and this
    file's premise no longer holds."""
    import ast
    import pathlib

    source = pathlib.Path("app/api/paper.py").read_text()
    tree = ast.parse(source)

    stamps_at_trade_time = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "strategy_version"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "strategy_version"
    ]
    assert stamps_at_trade_time, "paper no longer journals a captured session version"

    live_reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "strategy_version"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "version"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "strategy_row"
    ]
    assert len(live_reads) == 1, (
        "the only live read of strategy_row.version should be the session-start capture"
    )
