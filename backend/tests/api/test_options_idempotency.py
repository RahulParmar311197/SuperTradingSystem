"""An identical resubmit of a multi-leg options strategy double-filled it.

`POST /orders` has built its idempotency key from the request since round
206 -- `f"{user.id}:{symbol}:{direction}:{entry}:{stop}:{price}"` -- so an
identical resubmit dedupes onto the first order. `POST /options/execute`
minted `uuid.uuid4()` per request and fed it into every leg's key, so every
retry was a brand-new batch with brand-new keys, and nothing else on the
path deduped.

Measured through the real endpoint, one bull call spread of one lot
(lot_size 50) submitted twice:

    first  submit     : 201
    second submit     : 201
    batch ids differ  : True
    orders journalled : 4          (2 legs x 2 submissions)
    long leg          : quantity  100.0
    short leg         : quantity -100.0

Twice the spread and twice the premium, from a client doing the one thing
every HTTP client does after a timeout. After the fix the same pair of
calls leaves 2 orders, +/-50, and returns the same batch id.

`_execute_leg` already only submits to the broker when `create_order`
reports `created` -- the dedup machinery was there all along and was
defeated purely by the random id in the key.

Note on the harness: `option_snapshots` are NOT required to execute. A leg
with no snapshot adds a liquidity warning and is skipped for assessment,
so a behavioural options test needs only registered `Instrument` rows with
`market=OPTIONS`. Round 159 claimed otherwise when it justified covering
this endpoint structurally; that claim was wrong, and the exposure test at
the bottom of this file is the behavioural cover it should have had.
"""

import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.trading.persistence import persist_position
from app.trading.position_manager import PositionRecord

LOT = 50


async def _contract(kind: OptionType, strike: float) -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"OI{uuid.uuid4().hex[:8].upper()}",
            exchange="NSE",
            market=MarketType.OPTIONS,
            instrument_type="OPT",
            option_type=kind,
            strike=strike,
            lot_size=LOT,
            expiry=date.today() + timedelta(days=21),
            active=True,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _user(client) -> tuple[uuid.UUID, dict]:
    email = f"oi-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(
            id=uuid.uuid4(),
            email=email,
            password_hash=hash_password("testpass123"),
            name="Options Idem",
            trading_permissions=[TradingPermission.LIVE_TRADE.value],
        )
        db.add(row)
        await db.commit()
        user_id = row.id
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    return user_id, {"Authorization": f"Bearer {token}"}


def _spread(long_leg: Instrument, short_leg: Instrument, *, quantity: int = 1, premium: float = 5.0) -> dict:
    return {
        "strategy_name": "Bull call spread",
        "legs": [
            {"symbol": long_leg.symbol, "direction": "LONG", "quantity": quantity, "premium": premium},
            {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": quantity, "premium": 2.0},
        ],
    }


async def _counts(user_id: uuid.UUID) -> tuple[int, dict[str, float]]:
    async with async_session_factory() as db:
        orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
        rows = (
            await db.execute(
                select(Position, Instrument.symbol)
                .join(Instrument, Instrument.id == Position.instrument_id)
                .where(Position.user_id == user_id, Position.is_open.is_(True))
            )
        ).all()
    return len(orders), {symbol: float(p.quantity) for p, symbol in rows}


async def _cleanup(user_ids: list[uuid.UUID], instruments: list[Instrument]) -> None:
    from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS

    async with async_session_factory() as db:
        for user_id in user_ids:
            order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
            if order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
            await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
            await db.execute(delete(Order).where(Order.user_id == user_id))
            await db.execute(delete(Position).where(Position.user_id == user_id))
            for model in (Notification, RiskEvent, AuditLog, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        for row in instruments:
            await db.execute(delete(Instrument).where(Instrument.id == row.id))
        await db.commit()
    for user_id in user_ids:
        for cache in (_STACKS, _STACK_LOCKS, _TRADE_LOCKS):
            cache.pop(user_id, None)


# --- the finding ----------------------------------------------------------


async def test_an_identical_resubmit_does_not_double_fill(require_infra):
    """The headline, through the real endpoint. One lot asked for, one lot
    held, however many times the client retries."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            body = _spread(long_leg, short_leg)
            first = client.post("/options/execute", headers=headers, json=body)
            assert first.status_code == 201, first.text
            second = client.post("/options/execute", headers=headers, json=body)
            assert second.status_code == 201, second.text

            orders, quantities = await _counts(user_id)
            assert orders == 2, f"two legs, submitted twice, must journal two orders -- got {orders}"
            assert quantities == {long_leg.symbol: float(LOT), short_leg.symbol: -float(LOT)}, quantities
        finally:
            await _cleanup([user_id], [long_leg, short_leg])


async def test_the_retry_gets_the_same_batch_id_back(require_infra):
    """The response is idempotent too, not only the fills. A client that
    retries and reads a different batch id would reasonably conclude a
    second strategy exists, and go looking for it."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            body = _spread(long_leg, short_leg)
            first = client.post("/options/execute", headers=headers, json=body)
            second = client.post("/options/execute", headers=headers, json=body)
            assert first.json()["batch_id"] == second.json()["batch_id"]
        finally:
            await _cleanup([user_id], [long_leg, short_leg])


# --- what the fix must not break -----------------------------------------


async def test_a_genuinely_different_strategy_still_executes(require_infra):
    """The control that matters: a change that deduped everything would
    satisfy the proof above. A second submission differing in quantity is a
    different strategy and must go through."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            first = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg, quantity=1))
            assert first.status_code == 201, first.text
            second = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg, quantity=2))
            assert second.status_code == 201, second.text
            assert first.json()["batch_id"] != second.json()["batch_id"]

            orders, quantities = await _counts(user_id)
            assert orders == 4, f"two distinct strategies, two legs each -- got {orders}"
            assert quantities == {long_leg.symbol: 3.0 * LOT, short_leg.symbol: -3.0 * LOT}, quantities
        finally:
            await _cleanup([user_id], [long_leg, short_leg])


async def test_a_different_premium_is_a_different_strategy(require_infra):
    """Premium is part of the identity for the same reason `price` is on
    `POST /orders`: it decides what the trade costs."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            first = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg, premium=5.0))
            second = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg, premium=6.0))
            assert first.json()["batch_id"] != second.json()["batch_id"]
            orders, _ = await _counts(user_id)
            assert orders == 4, orders
        finally:
            await _cleanup([user_id], [long_leg, short_leg])


async def test_another_users_identical_strategy_is_not_deduped(require_infra):
    """Without the user id in the key, one account's spread would silently
    swallow another account's."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        first_user, first_headers = await _user(client)
        second_user, second_headers = await _user(client)
        try:
            body = _spread(long_leg, short_leg)
            a = client.post("/options/execute", headers=first_headers, json=body)
            b = client.post("/options/execute", headers=second_headers, json=body)
            assert a.status_code == 201 and b.status_code == 201, (a.text, b.text)
            assert a.json()["batch_id"] != b.json()["batch_id"]
            assert (await _counts(first_user))[0] == 2
            assert (await _counts(second_user))[0] == 2
        finally:
            await _cleanup([first_user, second_user], [long_leg, short_leg])


def test_the_batch_id_is_derived_not_random():
    """Pure, and the reason the two proofs above can hold: the same request
    must compute the same id in a fresh process, or a retry after a restart
    re-executes."""
    from app.api.options import _OPTIONS_BATCH_NAMESPACE

    user_id = uuid.uuid4()
    material = f"{user_id}:Bull call spread:AAA|LONG|1|5.0"
    assert uuid.uuid5(_OPTIONS_BATCH_NAMESPACE, material) == uuid.uuid5(_OPTIONS_BATCH_NAMESPACE, material)
    assert uuid.uuid5(_OPTIONS_BATCH_NAMESPACE, material) != uuid.uuid5(_OPTIONS_BATCH_NAMESPACE, material + "x")


# --- the behavioural cover round 159 should have had ---------------------


async def test_options_execution_counts_the_other_engines_exposure(require_infra):
    """Round 159 wired `POST /options/execute` into the account-wide
    exposure union and covered it STRUCTURALLY, on the stated grounds that
    exercising this endpoint needs fresh `option_snapshots` rows. That was
    wrong: a leg with no snapshot is simply not assessed, so the endpoint
    runs on registered contracts alone. This is the behavioural test.

    An auto-traded position holding 95% of the balance must make an options
    strategy that would add to it refuse on `exposure_limit`."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    elsewhere = await _contract(OptionType.PUT, 90.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            async with async_session_factory() as db:
                await persist_position(
                    db,
                    user_id,
                    elsewhere.id,
                    PositionRecord(
                        account_id=str(user_id), symbol=elsewhere.symbol, quantity=950.0,
                        average_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0,
                    ),
                    ExecutionMode.PAPER,
                    source_key="auto",
                )
                await db.commit()

            r = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg, quantity=40))
            assert r.status_code == 403, f"95% held by the auto loop plus this strategy exceeds the limit: {r.text}"
            assert "xposure" in r.text, r.text
        finally:
            await _cleanup([user_id], [long_leg, short_leg, elsewhere])
