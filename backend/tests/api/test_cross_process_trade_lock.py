"""The API process and the auto-trade worker raced each other's risk gates.

`serialize_user_trading` has held a per-user `asyncio.Lock` since round
152, and that makes ONE process correct. docker-compose.yml runs `api`
and `worker` as separate services, so it cannot reach
`AutoTradeSupervisor` at all: both read the account's exposure from
Postgres, neither sees the other's in-flight fill, and both approve.

Measured on `main` with the worker held provably mid-fill -- inside
`execution_engine.submit`, past its risk gate, position not yet in
Postgres -- against a 100,000 account with `max_exposure_pct` 100:

    before   Postgres exposure 0.00   POST /orders -> 201
             final: manual 80,000 + auto 31,212 = 111,212 = 111.2%
    control  worker settled first     SAME order  -> 403
             "Projected exposure 111.21% vs limit 100"      final 31.2%
    after    Postgres exposure 0.00   POST /orders -> 403, same reason

The fix is a Redis lock on the account, taken by both processes: Redis
is already this system's cross-process coordination layer (kill switch,
account halts, the atomic Lua rate limiter) and is durable
(`--appendonly yes`).

WHERE THE FIRST ATTEMPT WAS WRONG. The worker's first version released
the lock directly after `on_candle` returned, leaving `persist_position`
-- the write the other process's exposure gate actually reads -- outside
the critical section. Re-measured with the gate held in exactly that
window: the same manual order came back 201 and the account reached
111.21% again, i.e. the fix did not hold.
`test_the_lock_is_held_until_the_position_row_is_committed` is that
measurement.

WHY EVERY INTERLEAVE HERE IS FORCED WITH AN EVENT, NOT A SLEEP. Round
166's lesson: a race whose test depends on a sleep being long enough is
flaky on someone else's CI. Each gate below blocks on an `asyncio.Event`
the test releases itself, and `_first_of` lets the test proceed as soon
as EITHER the second actor reaches the lock (fixed world) or its request
has already finished (broken world) -- so a regression fails on an
assertion rather than hanging.

LIMITATION, DELIBERATELY NOT HIDDEN. The lock has an 8s TTL, so a holder
that outlives it loses it and the race returns. Held artificially longer
than the TTL during development, the 111.2% breach reappeared. Both
timing constants must also stay inside
`RiskLimits.market_data_max_staleness_seconds` (10.0), or the queueing
itself manufactures the staleness that rejects the trade -- an earlier
15s/20s pair produced exactly that ("Data age 15.03"). See
app/core/redis.py.
"""

import ast
import asyncio
import functools
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app.api import orders as orders_module
from app.auth.security import hash_password
from app.core.config import get_settings
from app.core.redis import _TRADE_LOCK_PREFIX, acquire_trade_lock, get_redis, release_trade_lock, set_latest_price
from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion
from app.database.models.trading import Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle
from app.workers import auto_trade_worker
from app.workers.auto_trade_worker import AutoTradeSupervisor

# Every wait in this file is bounded. A control that hangs reports
# nothing, and the regression guarded here is precisely the kind that can
# wedge an account's lock.
_DEADLINE = 60

# The account is MockBroker's 100,000 and `max_exposure_pct` is 100, so
# these are percentages of the limit as well as of the balance.
_BALANCE = 100_000.0

# 500 of risk (`risk_per_trade_pct` 0.5%) over 0.625 a share is 800
# shares at 100.0 -- 80,000, which fits under the limit by itself and
# breaches it once the worker's ~31,212 is counted too.
_MANUAL_ORDER = {"direction": "LONG", "entry": 100.0, "stop": 99.375}
_MANUAL_NOTIONAL = 80_000.0

# The same bullish sweep+FVG dataset every other auto-trade test uses:
# it matches on bar 8, which is where the entry is taken.
SETUP = [
    (100, 100, 99, 100), (100, 102, 100, 101), (101, 103, 100, 102), (102, 102, 97, 98),
    (98, 99, 96, 97), (97, 100, 96, 99), (99, 108, 99, 107), (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces into the FVG -> entry
]
LIQUID_BAR_VOLUME = 50_000.0


def _candles() -> list[Candle]:
    """Anchored recently so the supervisor's freshness gate (round 141)
    does not refuse the series."""
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)
    return [Candle(start + timedelta(minutes=i), o, h, l, c, LIQUID_BAR_VOLUME)
            for i, (o, h, l, c) in enumerate(SETUP)]


class _Gate:
    """Blocks whatever it wraps until the test lets it through.

    `entered` fires once the wrapped call is inside, so the test knows the
    other actor is provably at that point rather than hoping a sleep was
    long enough.
    """

    def __init__(self, real):
        self._real = real
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await self._real(*args, **kwargs)


class _LockWatch:
    """Wraps `acquire_trade_lock` to say when the API is BLOCKED on it.

    "Reached the lock" is not the signal a test can act on. Releasing the
    other actor as soon as the API arrives lets the two finish in either
    order, and a test built on it passed against an injected bug -- the
    worker simply won the resulting race. What distinguishes the two
    worlds is whether the lock was already held: one non-blocking attempt
    answers that outright, and only then does the real wait begin, so the
    call's semantics are unchanged.
    """

    def __init__(self, real, **overrides):
        self._real = real
        self._overrides = overrides
        self.blocked = asyncio.Event()

    async def __call__(self, account_id, **kwargs):
        merged = {**self._overrides, **kwargs}
        token = await self._real(account_id, **{**merged, "wait_seconds": 0})
        if token is not None:
            return token
        self.blocked.set()
        return await self._real(account_id, **merged)


async def _first_of(event: asyncio.Event, task: asyncio.Task) -> None:
    """Return as soon as `event` fires OR `task` finishes.

    In the fixed world the event fires: the request is queued behind a
    lock the other process holds, and cannot proceed until the test
    releases it. In a broken world it never fires, because the request was
    never blocked -- it has already been answered. Waiting only on the
    event would hang there, and a control that hangs reports nothing.
    Either way this returns and the assertions decide.
    """
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED, timeout=_DEADLINE)
    finally:
        waiter.cancel()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _instrument(prefix: str) -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(symbol=f"{prefix}{uuid.uuid4().hex[:6].upper()}", exchange="NSE",
                         market=MarketType.EQUITY, instrument_type="EQ")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _user(auto_symbol: str) -> tuple[uuid.UUID, str]:
    """One account that both trades manually and auto-trades -- which is
    the only way the two processes can collide on one exposure limit."""
    email = f"xpl-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(
            id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"),
            name="Cross Process", trading_permissions=[TradingPermission.LIVE_TRADE.value,
                                                       TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True, auto_trading_risk_per_trade_pct=1.0,
            auto_trading_max_positions=5, auto_trading_max_trades_per_day=5,
            # High enough that nothing here is refused for a reason other
            # than the exposure limit under test.
            auto_trading_daily_loss_limit_pct=50.0,
        )
        db.add(row)
        await db.flush()
        db.add(StrategyRow(
            user_id=row.id, name="Bullish FVG retest",
            definition={"name": "Bullish FVG retest", "market": auto_symbol, "timeframe": "15m",
                        "direction": "bullish",
                        "conditions": [{"type": "fvg", "direction": "bullish"}],
                        "entry": {"type": "fvg_retest"},
                        "risk": {"risk_percent": 1.0, "minimum_rr": 2.0}},
            is_active=True, eligible_for_auto_trading=True))
        await db.commit()
        return row.id, email


async def _headers(client, email: str) -> dict:
    r = await client.post("/auth/login", json={"email": email, "password": "testpass123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _feed_to_the_brink(supervisor, instrument_id: uuid.UUID, candles: list[Candle]) -> None:
    """Every bar but the one that takes the entry."""
    for candle in candles[:-1]:
        async with async_session_factory() as db:
            await upsert_candles(db, instrument_id, "15m", [candle])
        await supervisor.run_once()


async def _arm_entry_bar(instrument_id: uuid.UUID, candles: list[Candle]) -> None:
    async with async_session_factory() as db:
        await upsert_candles(db, instrument_id, "15m", [candles[-1]])


async def _order(client, headers, symbol: str, **overrides):
    """Priced immediately before the call: `market_data_max_staleness_seconds`
    is 10.0 and the fixture's candle feeding takes longer than that."""
    await set_latest_price(symbol, 100.0)
    return await client.post("/orders", headers=headers, json={"symbol": symbol, **_MANUAL_ORDER, **overrides})


async def _open_notional(user_id: uuid.UUID) -> float:
    async with async_session_factory() as db:
        rows = (await db.execute(select(Position).where(
            Position.user_id == user_id, Position.is_open.is_(True)))).scalars().all()
    return sum(abs(p.quantity) * p.average_price for p in rows)


async def _cleanup(user_id: uuid.UUID, instruments: list[Instrument]) -> None:
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        # Child rows first: `strategy_versions` references `strategies`.
        strategy_ids = (await db.execute(
            select(StrategyRow.id).where(StrategyRow.user_id == user_id))).scalars().all()
        if strategy_ids:
            await db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id.in_(strategy_ids)))
        for model in (Notification, RiskEvent, AuditLog, UserSession, StrategyRow):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        for row in instruments:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == row.id))
            await db.execute(delete(Instrument).where(Instrument.id == row.id))
        await db.commit()
    for cache in (orders_module._STACKS, orders_module._STACK_LOCKS, orders_module._TRADE_LOCKS):
        cache.pop(user_id, None)
    # The lock outlives the process that took it, by design -- so a test
    # that ends mid-hold would leak it into the next one.
    await get_redis().delete(f"{_TRADE_LOCK_PREFIX}{user_id}")


def _engine_of(supervisor, user_id: uuid.UUID, instrument_id: uuid.UUID):
    """The one engine for this (user, instrument).

    `_engines` is keyed (user_id, strategy_id, instrument_id) and the
    supervisor builds one per ACTIVE INSTRUMENT across every auto-trading
    user in this shared database -- hundreds of them. Taking
    `next(iter(...))`, or filtering on the user alone, patches a stranger's
    engine and reports a false negative; that happened while this was
    being measured.
    """
    matches = [engine for (uid, _sid, iid), engine in supervisor._engines.items()
               if uid == str(user_id) and iid == str(instrument_id)]
    assert len(matches) == 1, f"expected exactly one engine for this pair, got {len(matches)}"
    return matches[0]


# --- the finding ----------------------------------------------------------


async def test_a_manual_order_cannot_slip_past_the_gate_while_the_worker_is_mid_fill(require_infra):
    """The headline, both actors real, the interleave forced.

    The worker is stopped inside `execution_engine.submit`: past its own
    risk gate, with nothing in Postgres yet for the API's gate to see.
    """
    auto, manual = await _instrument("XPA"), await _instrument("XPM")
    user_id, email = await _user(auto.symbol)
    try:
        async with _client() as client:
            headers = await _headers(client, email)
            supervisor = AutoTradeSupervisor(timeframe="15m")
            candles = _candles()
            await _feed_to_the_brink(supervisor, auto.id, candles)

            engine = _engine_of(supervisor, user_id, auto.id)
            gate = _Gate(engine.execution_engine.submit)
            engine.execution_engine.submit = gate
            watch = _LockWatch(orders_module.acquire_trade_lock)
            orders_module.acquire_trade_lock = watch

            try:
                await _arm_entry_bar(auto.id, candles)
                worker = asyncio.create_task(supervisor.run_once())
                await asyncio.wait_for(gate.entered.wait(), timeout=_DEADLINE)

                # Nothing of the worker's fill is visible yet -- this is
                # the state the gate used to be evaluated against.
                assert await _open_notional(user_id) == 0.0

                order = asyncio.create_task(_order(client, headers, manual.symbol))
                await _first_of(watch.blocked, order)
                gate.release.set()

                response = await asyncio.wait_for(order, timeout=_DEADLINE)
                await asyncio.wait_for(worker, timeout=_DEADLINE)
            finally:
                orders_module.acquire_trade_lock = watch._real

            assert response.status_code == 403, response.text
            # The same verdict, with the same reason, the sequential
            # control below produces -- not merely "some refusal".
            assert "Projected exposure" in response.json()["detail"], response.text

            final = await _open_notional(user_id)
            assert final < _BALANCE, f"the account reached {final / _BALANCE:.1%} of a 100% limit"
            assert final < _MANUAL_NOTIONAL, "the manual order must not have opened at all"
    finally:
        await _cleanup(user_id, [auto, manual])


async def test_the_lock_is_held_until_the_position_row_is_committed(require_infra):
    """The first version of the fix released the lock as soon as
    `on_candle` returned, so the worker's `persist_position` -- the write
    the API's exposure gate reads -- still had not committed. Gating
    exactly that window reproduced the full 111.21% breach against the
    'fixed' code, which is why the critical section now covers the whole
    tail of the candle's processing.
    """
    auto, manual = await _instrument("XPA"), await _instrument("XPM")
    user_id, email = await _user(auto.symbol)
    real_persist = auto_trade_worker.persist_position
    try:
        async with _client() as client:
            headers = await _headers(client, email)
            supervisor = AutoTradeSupervisor(timeframe="15m")
            candles = _candles()
            await _feed_to_the_brink(supervisor, auto.id, candles)

            gate = _Gate(real_persist)
            auto_trade_worker.persist_position = gate
            watch = _LockWatch(orders_module.acquire_trade_lock)
            orders_module.acquire_trade_lock = watch

            try:
                await _arm_entry_bar(auto.id, candles)
                worker = asyncio.create_task(supervisor.run_once())
                await asyncio.wait_for(gate.entered.wait(), timeout=_DEADLINE)

                assert await _open_notional(user_id) == 0.0, (
                    "the gate must sit before the position row is committed"
                )

                order = asyncio.create_task(_order(client, headers, manual.symbol))
                await _first_of(watch.blocked, order)
                gate.release.set()

                response = await asyncio.wait_for(order, timeout=_DEADLINE)
                await asyncio.wait_for(worker, timeout=_DEADLINE)
            finally:
                orders_module.acquire_trade_lock = watch._real
                auto_trade_worker.persist_position = real_persist

            assert response.status_code != 201, (
                "the manual order was accepted while the worker's fill was uncommitted"
            )
            final = await _open_notional(user_id)
            assert final < _BALANCE, f"the account reached {final / _BALANCE:.1%} of a 100% limit"
    finally:
        await _cleanup(user_id, [auto, manual])


# --- controls -------------------------------------------------------------


async def test_the_same_order_is_refused_the_same_way_when_nothing_races(require_infra):
    """Control, and what makes the headline's 403 meaningful: run
    sequentially, the risk engine refuses this exact order with this exact
    reason. Without it, a 503 from the lock itself would read as success."""
    auto, manual = await _instrument("XPA"), await _instrument("XPM")
    user_id, email = await _user(auto.symbol)
    try:
        async with _client() as client:
            headers = await _headers(client, email)
            supervisor = AutoTradeSupervisor(timeframe="15m")
            candles = _candles()
            await _feed_to_the_brink(supervisor, auto.id, candles)
            await _arm_entry_bar(auto.id, candles)
            await asyncio.wait_for(supervisor.run_once(), timeout=_DEADLINE)

            settled = await _open_notional(user_id)
            assert settled > 0.0, "the worker must have opened a position for this control to mean anything"

            response = await asyncio.wait_for(
                _order(client, headers, manual.symbol), timeout=_DEADLINE)

            assert response.status_code == 403, response.text
            assert "Projected exposure" in response.json()["detail"], response.text
            assert await _open_notional(user_id) == settled
    finally:
        await _cleanup(user_id, [auto, manual])


async def test_an_order_that_still_fits_is_not_refused(require_infra):
    """Control against the over-fix. A lock that refused, deferred or
    dropped every manual order while auto-trading held a position would
    pass everything above and fail here."""
    auto, manual = await _instrument("XPA"), await _instrument("XPM")
    user_id, email = await _user(auto.symbol)
    try:
        async with _client() as client:
            headers = await _headers(client, email)
            supervisor = AutoTradeSupervisor(timeframe="15m")
            candles = _candles()
            await _feed_to_the_brink(supervisor, auto.id, candles)
            await _arm_entry_bar(auto.id, candles)
            await asyncio.wait_for(supervisor.run_once(), timeout=_DEADLINE)

            settled = await _open_notional(user_id)
            assert settled > 0.0

            # A tenth of the size: 80 shares at 100 is 8,000, which fits
            # beside the worker's position with room to spare.
            response = await asyncio.wait_for(
                _order(client, headers, manual.symbol, stop=93.75), timeout=_DEADLINE)

            assert response.status_code == 201, response.text
            assert await _open_notional(user_id) > settled, "the order must really have opened"
    finally:
        await _cleanup(user_id, [auto, manual])


async def test_the_worker_defers_its_candle_when_the_api_holds_the_lock(require_infra, monkeypatch):
    """The other direction. Unattended, there is no 503 to return, so
    failing closed means skipping the candle and taking it on the next
    pass -- one candle rather than a breached limit.

    The wait is shortened here so the test does not spend the full 8s
    queueing; the branch under test is the one that runs when the wait
    expires, not the length of the wait.
    """
    auto = await _instrument("XPA")
    user_id, email = await _user(auto.symbol)
    token = None
    try:
        monkeypatch.setattr(auto_trade_worker, "acquire_trade_lock",
                            functools.partial(acquire_trade_lock, wait_seconds=0.2))
        supervisor = AutoTradeSupervisor(timeframe="15m")
        candles = _candles()
        await _feed_to_the_brink(supervisor, auto.id, candles)
        await _arm_entry_bar(auto.id, candles)

        # Stand in for the API process holding it across a request.
        token = await acquire_trade_lock(str(user_id))
        assert token is not None

        await asyncio.wait_for(supervisor.run_once(), timeout=_DEADLINE)
        assert await _open_notional(user_id) == 0.0, (
            "the worker traded while another process held the account's lock"
        )

        # And the candle is not lost: the next pass takes it.
        await release_trade_lock(str(user_id), token)
        token = None
        await asyncio.wait_for(supervisor.run_once(), timeout=_DEADLINE)
        assert await _open_notional(user_id) > 0.0, "the deferred candle was never retried"
    finally:
        if token is not None:
            await release_trade_lock(str(user_id), token)
        await _cleanup(user_id, [auto])


async def test_the_api_answers_503_rather_than_trading_when_the_worker_holds_the_lock(require_infra, monkeypatch):
    """A caller that cannot get the lock is refused, with Retry-After --
    not 500, and not waved through. The wait is shortened for the same
    reason as the test above."""
    manual = await _instrument("XPM")
    user_id, email = await _user("UNUSED")
    token = None
    try:
        monkeypatch.setattr(orders_module, "acquire_trade_lock",
                            functools.partial(acquire_trade_lock, wait_seconds=0.2))
        async with _client() as client:
            headers = await _headers(client, email)
            token = await acquire_trade_lock(str(user_id))
            assert token is not None

            response = await asyncio.wait_for(
                _order(client, headers, manual.symbol), timeout=_DEADLINE)

            assert response.status_code == 503, response.text
            assert response.headers.get("Retry-After")
            assert await _open_notional(user_id) == 0.0
    finally:
        if token is not None:
            await release_trade_lock(str(user_id), token)
        await _cleanup(user_id, [manual])


async def test_a_refused_order_still_releases_the_cross_process_lock(require_infra):
    """The risk the new lock introduces rather than the one it removes.

    The handler raises `HTTPException(403)` from inside the critical
    section. Held past that, the account would be wedged for every
    process until the TTL expired. The Redis key is the instrument: the
    in-process `asyncio.Lock` unwinding says nothing about the one that
    crosses processes.
    """
    auto, manual = await _instrument("XPA"), await _instrument("XPM")
    user_id, email = await _user(auto.symbol)
    key = f"{_TRADE_LOCK_PREFIX}{user_id}"
    try:
        async with _client() as client:
            headers = await _headers(client, email)
            supervisor = AutoTradeSupervisor(timeframe="15m")
            candles = _candles()
            await _feed_to_the_brink(supervisor, auto.id, candles)
            await _arm_entry_bar(auto.id, candles)
            await asyncio.wait_for(supervisor.run_once(), timeout=_DEADLINE)

            first = await asyncio.wait_for(_order(client, headers, manual.symbol), timeout=_DEADLINE)
            assert first.status_code == 403, first.text
            assert await get_redis().get(key) is None, "the refusal left the account's lock held"

            # And the next caller is answered rather than blocked.
            second = await asyncio.wait_for(_order(client, headers, manual.symbol), timeout=_DEADLINE)
            assert second.status_code == 403, second.text
    finally:
        await _cleanup(user_id, [auto, manual])


async def test_a_redis_outage_refuses_by_default_and_opens_only_when_configured(require_infra, monkeypatch):
    """Round 129's call, applied to this dependency: a risk gate that
    cannot be evaluated refuses rather than guesses. Both directions are
    asserted, because a fix that hard-coded either one would pass a
    single-sided test."""
    manual = await _instrument("XPM")
    user_id, email = await _user("UNUSED")
    try:
        async def _dead(*args, **kwargs):
            raise ConnectionError("Redis is down")

        monkeypatch.setattr(orders_module, "acquire_trade_lock", _dead)
        settings = get_settings()
        assert settings.trade_lock_fail_open is False, "the default must be fail-closed"

        async with _client() as client:
            headers = await _headers(client, email)

            refused = await asyncio.wait_for(_order(client, headers, manual.symbol), timeout=_DEADLINE)
            assert refused.status_code == 503, refused.text
            assert refused.headers.get("Retry-After")
            assert await _open_notional(user_id) == 0.0

            monkeypatch.setattr(settings, "trade_lock_fail_open", True)
            allowed = await asyncio.wait_for(_order(client, headers, manual.symbol), timeout=_DEADLINE)
            assert allowed.status_code == 201, allowed.text
    finally:
        await _cleanup(user_id, [manual])


# --- structural -----------------------------------------------------------


def test_every_process_that_can_open_a_position_takes_the_account_lock():
    """Structural, and labelled as such: it asserts the SET of callers.

    The in-process `asyncio.Lock` was correct for one service and useless
    across two, so what matters is that every process which can move this
    account's exposure participates. `/paper` is deliberately absent: a
    sandbox neither reads nor writes the account's book (round 165), so
    locking it would serialize it against real trading for nothing.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    candidates = {
        "app/api/orders.py": True,
        "app/api/options.py": True,
        "app/workers/auto_trade_worker.py": True,
        "app/api/paper.py": False,
    }

    takes = {}
    for relative in candidates:
        tree = ast.parse((root.parent / relative).read_text())
        # The CALL, not the imported name: `app/api/options.py` reaches
        # it through `Depends(serialize_user_trading)` while the worker
        # calls `acquire_trade_lock` itself, and a leftover import with
        # the use deleted must not satisfy either.
        takes[relative] = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and (
                node.func.id == "acquire_trade_lock"
                or (node.func.id == "Depends" and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "serialize_user_trading")
            )
            for node in ast.walk(tree)
        )

    assert takes == candidates, f"processes taking the account lock: {takes}"


# --- the lock's own contract ----------------------------------------------


async def test_releasing_a_lapsed_lock_cannot_delete_the_next_holders(require_infra, monkeypatch):
    """Helper-level, and deliberately so: this is the one guarantee no
    end-to-end test above can reach.

    The TTL exists so a crashed holder cannot wedge an account forever,
    which means a slow holder can lose the lock while still believing it
    holds it. If release were a plain `DEL`, that holder would delete
    whoever legitimately took it next, and both processes would then run
    their risk gates concurrently again -- the original bug, reintroduced
    by the mechanism meant to fix it. The Lua compare-and-delete is what
    prevents it.
    """
    account = str(uuid.uuid4())
    key = f"{_TRADE_LOCK_PREFIX}{account}"
    try:
        monkeypatch.setattr("app.core.redis.TRADE_LOCK_TTL_SECONDS", 1)
        lapsed = await acquire_trade_lock(account)
        assert lapsed is not None

        # Wait for the TTL rather than sleeping a guessed interval: the
        # key's own disappearance is the signal.
        async def _expired() -> None:
            while await get_redis().exists(key):
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_expired(), timeout=_DEADLINE)

        successor = await acquire_trade_lock(account)
        assert successor is not None and successor != lapsed

        await release_trade_lock(account, lapsed)

        assert await get_redis().get(key) is not None, (
            "a lapsed holder's release destroyed the successor's lock"
        )
        await release_trade_lock(account, successor)
        assert await get_redis().get(key) is None, "the real holder could not release"
    finally:
        await get_redis().delete(key)
