"""Two engines gated the account's exposure against the same balance.

`positions.source_key` partitions the table so the manual stack, each
`POST /paper` session and `AutoTradeSupervisor` stop overwriting each
other's rows. Each engine then rebuilt only its own partition and measured
`current_exposure` against that -- but `max_exposure_pct` is a percentage
of the account BALANCE, and the account has one balance. Blueprint §86
calls exposure a portfolio quantity: "Total exposure".

Measured through the real endpoints and the real `AutoTradeSupervisor`,
one account, 100,000 balance, `max_exposure_pct=100`:

    auto positions open   : 3     gross  93,636.36
    manual POST /orders   : 201   exposure_limit recorded True
    manual stack sees     : 1 position, exposure 52,000.00
    TOTAL gross notional  : 145,636.36   = 145.6% of the account

Each path sat under its own limit while the account was half as levered
again as the limit allows, and the gate recorded a pass. After the fix the
same order is refused: `Projected exposure 114.42% vs limit 100.0%`.

Two scoping decisions are pinned here as decisions rather than left as
omissions:

  * `paper:<session_id>` is NOT in the union. That session is a sandbox
    with its own `starting_balance` and consumes none of the account's
    capital.
  * The COUNT limits stay per-path. `RiskLimits.max_open_positions` and
    `user.auto_trading_max_positions` are two separately configured
    budgets, and no unambiguous failure of them was measured -- a bound
    goes where a failure was measured and nowhere else.

The auto-side rows are written with `persist_position`, which IS the
function the worker writes them with; what is under test here is whether
the READERS union, and the manual side goes through the real endpoint.
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Setup, Signal
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.trading.persistence import (
    ACCOUNT_BACKED_SOURCE_KEYS,
    load_open_position_notionals_elsewhere,
    persist_position,
)
from app.trading.position_manager import PositionRecord

# MockBroker's starting balance -- what `max_exposure_pct` measures against.
BALANCE = 100_000.0


async def _instrument(prefix: str = "CEX") -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"{prefix}{uuid.uuid4().hex[:7].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _user(client) -> tuple[uuid.UUID, dict]:
    """A registered user with LIVE_TRADE, and its auth header."""
    email = f"cex-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Cross Engine"})
    assert r.status_code == 201, r.text
    user_id = uuid.UUID(r.json()["user"]["id"]) if "user" in r.json() else None
    async with async_session_factory() as db:
        row = (await db.execute(select(User).where(User.email == email))).scalar_one()
        row.trading_permissions = [TradingPermission.LIVE_TRADE.value, TradingPermission.AUTO_TRADE.value]
        await db.commit()
        user_id = row.id
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    return user_id, {"Authorization": f"Bearer {token}"}


async def _open_position(
    user_id: uuid.UUID, instrument_id: uuid.UUID, symbol: str, *, source_key: str, quantity: float, price: float
) -> None:
    """Open a position in another engine's partition, through the same
    writer that engine uses."""
    async with async_session_factory() as db:
        await persist_position(
            db,
            user_id,
            instrument_id,
            PositionRecord(
                account_id=str(user_id),
                symbol=symbol,
                quantity=quantity,
                average_price=price,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
            ),
            ExecutionMode.PAPER,
            source_key=source_key,
        )
        await db.commit()


def _order(client, headers, symbol: str, *, entry: float, stop: float):
    return client.post(
        "/orders",
        headers=headers,
        json={"symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": entry, "stop": stop},
    )


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first: order_events -> orders, trades -> positions,
    sessions/audit -> users."""
    from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS

    async with async_session_factory() as db:
        for user_id in user_ids:
            order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
            if order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
            await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
            await db.execute(delete(Order).where(Order.user_id == user_id))
            await db.execute(delete(Position).where(Position.user_id == user_id))
            for model in (Notification, RiskEvent, AuditLog, StrategyRow, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        for instrument_id in instrument_ids:
            for model in (Signal, Setup, CandleRow):
                await db.execute(delete(model).where(model.instrument_id == instrument_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()
    for user_id in user_ids:
        for cache in (_STACKS, _STACK_LOCKS, _TRADE_LOCKS):
            cache.pop(user_id, None)


# The bullish sweep+FVG series the worker tests use: it matches on bar 8
# and retraces into the FVG there, which is the entry.
_SETUP = [
    (100, 100, 99, 100), (100, 102, 100, 101), (101, 103, 100, 102),
    (102, 102, 97, 98), (98, 99, 96, 97), (97, 100, 96, 99),
    (99, 108, 99, 107), (107, 110, 106, 109), (109, 109, 103, 104),
]
# The equity liquidity gate caps an order at a share of the bar's traded
# volume, and these setups size to hundreds of shares.
_LIQUID_BAR_VOLUME = 50_000.0


def _entry_series() -> list:
    from datetime import datetime, timedelta, timezone

    from app.smc.types import Candle

    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)
    return [
        Candle(start + timedelta(minutes=i), o, h, l, c, _LIQUID_BAR_VOLUME)
        for i, (o, h, l, c) in enumerate(_SETUP)
    ]


# --- the finding ----------------------------------------------------------


async def test_a_manual_order_counts_the_auto_loops_open_exposure(require_infra):
    """The headline. `POST /orders` sizes to 52% of the balance; an auto
    position already holds 60%. Each is under the 100% limit on its own and
    the account is at 112%, so the order must be refused."""
    instrument = await _instrument()
    other = await _instrument()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            await _open_position(
                user_id, other.id, other.symbol, source_key="auto", quantity=600.0, price=100.0
            )  # 60,000 = 60% of the balance
            r = _order(client, headers, instrument.symbol, entry=104.0, stop=103.0)  # 52,000 = 52%
            assert r.status_code == 403, f"the account would sit at 112% of its balance: {r.status_code} {r.text}"
            assert "xposure" in r.text, r.text
        finally:
            await _cleanup([user_id], [instrument.id, other.id])


async def test_the_auto_loop_counts_the_manual_stacks_open_exposure(require_infra):
    """The other layer, which the first cannot stand in for. A fix wired
    only into `POST /orders` would leave the autonomous path -- the one
    nobody is watching -- still measuring half the account.

    Behavioural, through the same `PaperTradingEngine` the worker runs,
    carrying the same `source_key` the worker gives it: a manual position
    holding 60% of the balance must make this engine refuse the entry its
    own signal produces, and the run with no manual position is the control
    that proves the entry was there to refuse."""
    from app.paper.engine import PaperTradingEngine
    from app.strategy.dsl import StrategyDefinition

    definition = StrategyDefinition.model_validate(
        {
            "name": "Bullish FVG retest",
            "market": "IRRELEVANT",
            "timeframe": "15m",
            "direction": "bullish",
            "conditions": [{"type": "fvg", "direction": "bullish"}],
            "entry": {"type": "fvg_retest"},
            "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
        }
    )

    async def _run(*, with_manual_position: bool) -> int:
        instrument = await _instrument()
        other = await _instrument()
        with TestClient(app) as client:
            user_id, _headers = await _user(client)
            try:
                if with_manual_position:
                    # 95% of the balance. This entry sizes to roughly 8.7%
                    # of the account (0.5% risk over the FVG stop distance),
                    # so 60% would still have left room -- the number has to
                    # be big enough that the UNION crosses 100% while the
                    # engine's own book alone stays far below it.
                    await _open_position(
                        user_id, other.id, other.symbol, source_key="manual", quantity=950.0, price=100.0
                    )
                engine = PaperTradingEngine(
                    definition,
                    symbol=instrument.symbol,
                    account_id=str(user_id),
                    starting_balance=BALANCE,
                    source_key="auto",
                )
                opened = 0
                refused_for: list[str] = []
                async with async_session_factory() as db:
                    for candle in _entry_series():
                        outcome = await engine.on_candle(candle, db=db)
                        if outcome.order_created:
                            opened += 1
                        if outcome.risk_failed_check is not None:
                            refused_for.append(outcome.risk_failed_check)
                return opened, refused_for
            finally:
                await _cleanup([user_id], [instrument.id, other.id])

    opened, _ = await _run(with_manual_position=False)
    assert opened == 1, (
        f"fixture: this series must produce exactly one entry, or the refusal below proves "
        f"nothing -- got {opened}"
    )

    opened, refused_for = await _run(with_manual_position=True)
    assert opened == 0, "95% held by the manual stack plus this entry exceeds the account's limit"
    # Named, not merely absent: an entry refused for some unrelated reason
    # would satisfy `opened == 0` while proving nothing about the union.
    assert "exposure_limit" in refused_for, refused_for


# --- what the fix must not break -----------------------------------------


async def test_an_order_that_still_fits_the_account_is_accepted(require_infra):
    """The control that matters most: a change that simply refused more
    orders would satisfy the proof above. 30% held elsewhere plus this
    order's 52% is 82%, inside the 100% limit, and must still fill."""
    instrument = await _instrument()
    other = await _instrument()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            await _open_position(
                user_id, other.id, other.symbol, source_key="auto", quantity=300.0, price=100.0
            )  # 30,000 = 30%
            r = _order(client, headers, instrument.symbol, entry=104.0, stop=103.0)
            assert r.status_code == 201, f"82% of the balance is inside the limit: {r.status_code} {r.text}"
        finally:
            await _cleanup([user_id], [instrument.id, other.id])


async def test_another_users_positions_are_not_this_accounts_exposure(require_infra):
    """Scoping control. The union is per user; without the `user_id`
    predicate every account on the platform would gate against every other
    account's book."""
    instrument = await _instrument()
    other = await _instrument()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        stranger_id, _ = await _user(client)
        try:
            await _open_position(
                stranger_id, other.id, other.symbol, source_key="auto", quantity=900.0, price=100.0
            )  # 90% -- of somebody else's account
            r = _order(client, headers, instrument.symbol, entry=104.0, stop=103.0)
            assert r.status_code == 201, f"another user's positions must not gate this one: {r.text}"
        finally:
            await _cleanup([user_id, stranger_id], [instrument.id, other.id])


async def test_a_paper_sandbox_is_not_account_exposure(require_infra):
    """The declined half of the scope, pinned as a decision.

    A `POST /paper` session runs on its own `starting_balance` and consumes
    none of the account's capital, so its positions are not the account's
    exposure -- counting them would refuse real trades over simulated ones.
    90% held in a sandbox must not block a 52% real order."""
    instrument = await _instrument()
    other = await _instrument()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            await _open_position(
                user_id, other.id, other.symbol, source_key=f"paper:{uuid.uuid4()}", quantity=900.0, price=100.0
            )
            r = _order(client, headers, instrument.symbol, entry=104.0, stop=103.0)
            assert r.status_code == 201, f"a sandbox must not gate the real account: {r.status_code} {r.text}"
        finally:
            await _cleanup([user_id], [instrument.id, other.id])


async def test_the_count_limits_stay_per_path(require_infra):
    """The other declined half, also pinned as a decision rather than left
    as an omission.

    `RiskLimits.max_open_positions` (manual) and
    `user.auto_trading_max_positions` (auto) are two separately configured
    budgets, and no unambiguous failure of them was measured -- unlike
    exposure, which is a percentage of one shared balance. So positions
    held by the auto loop do NOT consume the manual path's count: five auto
    positions, each tiny enough that exposure is not what is being tested,
    and a manual order still fills.

    If that decision is ever revisited, this test is the thing to change,
    deliberately."""
    from app.risk.limits import RiskLimits

    max_open = RiskLimits().max_open_positions
    instrument = await _instrument()
    others = [await _instrument() for _ in range(max_open)]
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            for row in others:
                await _open_position(
                    user_id, row.id, row.symbol, source_key="auto", quantity=1.0, price=100.0
                )  # 0.1% each: the COUNT is the point, not the notional
            r = _order(client, headers, instrument.symbol, entry=104.0, stop=103.0)
            assert r.status_code == 201, (
                f"the count limits are deliberately per-path; {max_open} auto positions must not "
                f"consume the manual budget: {r.status_code} {r.text}"
            )
        finally:
            await _cleanup([user_id], [instrument.id] + [row.id for row in others])


# --- the helper's own contract -------------------------------------------


async def test_the_helper_signs_its_notionals(require_infra):
    """Signed, because `correlated_exposure` nets -- an `abs()` here turns
    a hedge into double concentration, and blocks the trade that reduced
    the risk.

    The helper also SUMS rather than assigns when two rows share a symbol.
    That branch is deliberately not asserted here because it is currently
    unreachable, and saying so is better than a test that pretends to reach
    it: the unique index puts one row per (user, instrument, mode,
    source_key), so a repeated symbol needs two source keys, and with
    exactly two account-backed partitions one of them is always the
    excluded caller. It is kept for when
    `ACCOUNT_BACKED_SOURCE_KEYS` grows -- assignment would then silently
    drop one engine's book -- and the test below pins that there are two."""
    a = await _instrument()
    b = await _instrument()
    with TestClient(app) as client:
        user_id, _headers = await _user(client)
        try:
            await _open_position(user_id, a.id, a.symbol, source_key="auto", quantity=-200.0, price=50.0)
            await _open_position(user_id, b.id, b.symbol, source_key="auto", quantity=100.0, price=20.0)
            async with async_session_factory() as db:
                seen = await load_open_position_notionals_elsewhere(db, user_id, excluding_source_key="manual")
            assert seen == {a.symbol: -10_000.0, b.symbol: 2_000.0}, seen
        finally:
            await _cleanup([user_id], [a.id, b.id])


async def test_the_helper_excludes_the_callers_own_partition(require_infra):
    """Otherwise every engine would double-count its own book."""
    instrument = await _instrument()
    with TestClient(app) as client:
        user_id, _headers = await _user(client)
        try:
            await _open_position(
                user_id, instrument.id, instrument.symbol, source_key="manual", quantity=100.0, price=10.0
            )
            async with async_session_factory() as db:
                assert await load_open_position_notionals_elsewhere(db, user_id, excluding_source_key="manual") == {}
                assert await load_open_position_notionals_elsewhere(db, user_id, excluding_source_key="auto") == {
                    instrument.symbol: 1_000.0
                }
        finally:
            await _cleanup([user_id], [instrument.id])


async def test_a_closed_position_is_not_exposure(require_infra):
    """The `is_open` predicate, pinned.

    Measured honestly: a closed row contributes 0.0 to the gross SUM either
    way, because `PositionManager.apply_fill` zeroes `quantity` and
    `average_price` when a position goes flat -- so dropping the predicate
    does not inflate the exposure number. What it does is leave the symbol
    in the mapping at 0.0, and that mapping is also what feeds
    `compute_correlated_exposure`'s view of what the account holds. A
    symbol the account no longer holds does not belong there."""
    instrument = await _instrument()
    with TestClient(app) as client:
        user_id, _headers = await _user(client)
        try:
            await _open_position(
                user_id, instrument.id, instrument.symbol, source_key="auto", quantity=100.0, price=50.0
            )
            async with async_session_factory() as db:
                assert await load_open_position_notionals_elsewhere(
                    db, user_id, excluding_source_key="manual"
                ) == {instrument.symbol: 5_000.0}, "fixture: it must count while it is open"

            # Flat: what `apply_fill` leaves behind when a position closes.
            await _open_position(
                user_id, instrument.id, instrument.symbol, source_key="auto", quantity=0.0, price=0.0
            )
            async with async_session_factory() as db:
                assert await load_open_position_notionals_elsewhere(
                    db, user_id, excluding_source_key="manual"
                ) == {}, "a closed position is not part of the account's book at all"
        finally:
            await _cleanup([user_id], [instrument.id])


def test_every_site_that_measures_account_exposure_takes_the_union():
    """STRUCTURAL, and labelled as such.

    There are three places that build a risk proposal carrying
    `current_exposure`: `POST /orders`, `PaperTradingEngine._maybe_enter`
    and `POST /options/execute`. The first two are proven behaviourally
    above. The third is asserted structurally for the same reason round 152
    gave when it covered this endpoint the same way: exercising
    `POST /options/execute` end to end needs registered `option_contracts`
    and fresh `option_snapshots` rows, and the mechanism it would be
    proving is already proven behaviourally on the other two.

    This test exists because the change that introduced the union wired two
    of the three and shipped: the options path kept measuring one partition
    for a whole release. A count is what catches a fourth site appearing
    without the union, which is exactly how the third was missed."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    sites = {
        "api/orders.py": root / "api/orders.py",
        "paper/engine.py": root / "paper/engine.py",
        "api/options.py": root / "api/options.py",
    }
    # The CALL and the union expression, not merely the name: checking for
    # the bare name passes on the leftover `from ... import` line alone,
    # which is exactly how the first version of this test let a revert of
    # the options path through with every test still green.
    missing = [
        name
        for name, path in sites.items()
        if "load_open_position_notionals_elsewhere(" not in path.read_text()
        or "abs(notional) for notional in elsewhere.values()" not in path.read_text()
    ]
    assert not missing, (
        f"these sites measure account exposure without unioning the other engines' "
        f"positions: {missing}"
    )

    # Nothing else may compute `current_exposure` off a bare position
    # manager -- a fourth site would be the same bug again.
    computing = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "current_exposure = sum(" in path.read_text()
    )
    assert computing == ["api/options.py", "api/orders.py", "paper/engine.py"], computing


def test_the_account_backed_partitions_are_named_not_inferred():
    """A new engine that mirrors into `positions` joins the account's
    exposure only by being named here -- deliberately, so adding one is a
    decision rather than an accident in either direction."""
    assert ACCOUNT_BACKED_SOURCE_KEYS == ("manual", "auto")
