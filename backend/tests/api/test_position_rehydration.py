"""A restarted process must not start flat.

`OrderManager`/`PositionManager` live in one process's memory and every
fill is mirrored into `positions` -- but nothing ever read that mirror
back. A restart therefore began with an empty book while the account's
real positions sat open in Postgres, and the risk gates are computed *from
that book*: `current_exposure` and `max_open_positions` sum it,
`correlated_exposure` reads it, and `is_reducing` -- the exemption that
lets a position always be closed -- looks the position up in it.

Measured before the fix, on a single open position of 100 @ 100:
`GET /positions` returned `[]` and exposure summed to 0.00 while the row
was open in the database.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import orders as orders_mod
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order as OrderRow
from app.database.models.trading import OrderEvent as OrderEventRow
from app.database.models.trading import Position as PositionRow
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register_and_grant(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"rehydrate-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Rehydrate"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers
    )
    assert r.status_code == 200, r.text

    from app.auth.security import TokenType, decode_token

    return headers, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _make_instrument() -> tuple[uuid.UUID, str]:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"RHY{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
            lot_size=1,
            tick_size=0.05,
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id, instrument.symbol


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        order_ids = (
            await db.execute(select(OrderRow.id).where(OrderRow.user_id == user_id))
        ).scalars().all()
        for order_id in order_ids:
            await db.execute(delete(OrderEventRow).where(OrderEventRow.order_id == order_id))
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(OrderRow).where(OrderRow.user_id == user_id))
        await db.execute(delete(PositionRow).where(PositionRow.user_id == user_id))
        await db.execute(delete(RiskEvent).where(RiskEvent.user_id == user_id))
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


def _restart_the_process() -> None:
    """`_STACKS` is process-local state keyed by user; dropping it is
    exactly what a process restart does to the in-memory book."""
    orders_mod._STACKS.clear()
    orders_mod._STACK_LOCKS.clear()


async def test_an_open_position_survives_a_restart(require_infra):
    """Behavioural proof. Open a position, discard the in-memory stacks,
    and require the rebuilt stack to agree with the database about both
    the position and the exposure the risk gates are computed from.
    """
    instrument_id, symbol = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register_and_grant(client)
        try:
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                rows = (
                    await db.execute(
                        select(PositionRow).where(
                            PositionRow.user_id == user_id, PositionRow.is_open.is_(True)
                        )
                    )
                ).scalars().all()
            assert len(rows) == 1, "the fixture did not actually open a position"
            expected_exposure = abs(float(rows[0].quantity) * float(rows[0].average_price))
            assert expected_exposure > 0

            _restart_the_process()

            r = client.get("/positions", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body) == 1, f"the position vanished across the restart: {body}"
            assert body[0]["symbol"] == symbol

            stack = orders_mod._STACKS[user_id]
            restored = stack.position_manager.open_positions(str(user_id))
            assert len(restored) == 1
            exposure = sum(abs(p.quantity) * p.average_price for p in restored)
            assert exposure == pytest.approx(expected_exposure), (
                "the rebuilt book disagrees with the database about exposure, which is "
                "what exposure_limit and max_open_positions are computed from"
            )
        finally:
            _restart_the_process()
            await _cleanup(user_id, instrument_id)


async def test_rehydrated_values_are_floats_not_decimals(require_infra):
    """Behavioural proof of the type half, which is a separate failure.

    The `positions` columns are `Numeric(18, 6)`, which SQLAlchemy returns
    as `Decimal` whatever the `Mapped[float]` annotation says, while
    `PositionRecord` is float throughout. Restoring the raw column values
    would put `Decimal` into the book and the next fill would raise
    `TypeError: unsupported operand type(s) for *: 'decimal.Decimal' and
    'float'` inside `apply_fill`. Asserting the types directly is what
    keeps the `float()` casts in `load_open_positions` from being
    "cleaned up" later.
    """
    instrument_id, symbol = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register_and_grant(client)
        try:
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            _restart_the_process()
            client.get("/positions", headers=headers)

            restored = orders_mod._STACKS[user_id].position_manager.open_positions(str(user_id))
            assert restored, "nothing was restored, so this proves nothing"
            for position in restored:
                assert type(position.quantity) is float, type(position.quantity)
                assert type(position.average_price) is float, type(position.average_price)
                # The arithmetic that would actually raise.
                assert isinstance(position.average_price * position.quantity + 1.5 * 2.0, float)
        finally:
            _restart_the_process()
            await _cleanup(user_id, instrument_id)


async def test_a_user_with_no_positions_still_gets_a_working_stack(require_infra):
    """Control. Rehydration runs on every stack build, including for an
    account that has never traded -- that must produce an empty book and a
    usable stack, not an error or a phantom position.
    """
    with TestClient(app) as client:
        headers, user_id = await _register_and_grant(client)
        try:
            r = client.get("/positions", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json() == []
            assert orders_mod._STACKS[user_id].position_manager.open_positions(str(user_id)) == []
        finally:
            _restart_the_process()
            async with async_session_factory() as db:
                await db.execute(delete(Notification).where(Notification.user_id == user_id))
                await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
                await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
