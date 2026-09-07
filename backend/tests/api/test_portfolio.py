import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        for order_id in order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Trade).where(Trade.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        await db.execute(delete(RiskEvent).where(RiskEvent.user_id == user_id))
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_portfolio_reports_real_exposure_for_a_paper_account(require_infra):
    # Regression test: GET /portfolio had never been exercised by any
    # test. It queries the `positions` table filtered by
    # execution_mode=LIVE by default -- for a user with no connected
    # broker (this one), positions are correctly persisted as PAPER, so
    # without passing the caller's actual execution mode through, this
    # would silently report zero exposure for every such account.
    with TestClient(app) as client:
        email = f"portfolio-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Portfolio Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
        assert r.status_code == 200, r.text

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"PORT{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert position_row.execution_mode == ExecutionMode.PAPER

            r = client.get("/portfolio", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["open_position_count"] == 1
            assert body["total_exposure"] == pytest.approx(10000.0, rel=1e-6)  # 100 shares * 100
            assert body["exposure_by_market"].get("EQUITY") == pytest.approx(10000.0, rel=1e-6)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_portfolio_reports_realized_pnl_after_a_position_closes(require_infra):
    # Regression test: `total_realized_pnl` was summed over
    # `position_manager.open_positions(...)`. Realized P&L exists precisely
    # *because* a position closed, so filtering to open positions reported
    # 0.0 for every fully closed trade -- the user made money and the
    # portfolio said zero. Worse, on a partial close it reported only what
    # had been realized so far, so closing the remainder drove the number
    # back *down* to zero.
    #
    # The two existing tests here both hit GET /portfolio and both pass,
    # but neither ever closes a position and neither asserts anything about
    # total_realized_pnl -- which is why this survived.
    with TestClient(app) as client:
        email = f"realized-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Realized Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
        assert r.status_code == 200, r.text

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"RPNL{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # Open: POST /orders sizes quantity itself from risk_per_trade_pct
            # and |entry - stop|, so the caller never picks it.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                opened_quantity = float(position_row.quantity)
            assert opened_quantity > 0

            assert client.get("/portfolio", headers=headers).json()["total_realized_pnl"] == pytest.approx(0.0)

            # Close the whole position at a profit. POST /orders has no
            # `quantity` field -- it sizes every order as
            # `balance * risk_per_trade_pct / abs(entry - stop)` -- so an
            # equal-width stop (5, matching the opening order's 100/95)
            # produces an equal quantity and closes the position fully.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 110.0, "stop": 115.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                persisted_realized = float(position_row.realized_pnl)
                assert position_row.is_open is False, "expected a flat position after closing the full quantity"
            assert persisted_realized > 0, "the close should have realized a profit"

            body = client.get("/portfolio", headers=headers).json()
            assert body["open_position_count"] == 0
            # The whole point: a closed, profitable trade must not report 0.0.
            assert body["total_realized_pnl"] == pytest.approx(persisted_realized, rel=1e-6)
            assert body["total_realized_pnl"] > 0
        finally:
            await _cleanup(user_id, instrument_id)


async def test_portfolio_realized_pnl_is_not_double_counted_across_re_entries(require_infra):
    # Regression test: `total_realized_pnl` summed `Position.realized_pnl`
    # over every row for the account. That column is not an increment --
    # `PositionManager.apply_fill` keeps one `PositionRecord` per (account,
    # symbol) forever and does `realized_pnl += realized`, so it is a
    # lifetime running total, and `persist_position` looks up only the
    # *open* row, so re-entering an instrument inserts a NEW row seeded
    # with that lifetime total. Summing the rows counted every earlier
    # round trip again: two closes worth +p1 and +p2 reported 2*p1 + p2.
    #
    # The single-round-trip test above cannot see this -- one instrument,
    # one row, and `scalar_one()` structurally asserts there is only ever
    # one -- and it compares the endpoint against that same row's value, so
    # `SUM(realized_pnl) == row.realized_pnl` held regardless.
    with TestClient(app) as client:
        email = f"realized2-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Realized Twice"})
        assert r.status_code == 201, r.text
        token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
        assert client.post(
            "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers
        ).status_code == 200

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"RPNL2{uuid.uuid4().hex[:5].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        async def _open_quantity() -> float:
            # Filtered to the open row on purpose: after the second entry
            # there are two rows for this instrument, so `scalar_one()`
            # over all of them -- what every other position test uses --
            # would raise here.
            async with async_session_factory() as db:
                row = (
                    await db.execute(
                        select(Position).where(Position.user_id == user_id, Position.is_open.is_(True))
                    )
                ).scalar_one()
                return float(row.quantity)

        try:
            # Two full round trips on the SAME instrument. MockBroker fills
            # at the submitted entry, and every order is server-sized from
            # `balance * risk_per_trade_pct / abs(entry - stop)`, so an
            # equal-width stop on the closing order closes the position
            # fully. Expected P&L is computed from the quantity actually
            # opened and the prices chosen here -- never from the
            # aggregation under test.
            # Distinct prices per round trip: POST /orders keys idempotency
            # on `{user}:{symbol}:{direction}:{entry}:{stop}`, so re-entering
            # at the same entry/stop is deduped into the first order and no
            # second position is ever opened.
            expected = 0.0
            for entry_in, exit_out in ((100.0, 110.0), (102.0, 108.0)):
                assert client.post(
                    "/orders",
                    json={"symbol": instrument.symbol, "direction": "LONG", "entry": entry_in, "stop": entry_in - 5},
                    headers=headers,
                ).status_code == 201
                quantity = await _open_quantity()
                assert client.post(
                    "/orders",
                    json={"symbol": instrument.symbol, "direction": "SHORT", "entry": exit_out, "stop": exit_out + 5},
                    headers=headers,
                ).status_code == 201
                expected += quantity * (exit_out - entry_in)

            async with async_session_factory() as db:
                rows = (await db.execute(select(Position).where(Position.user_id == user_id))).scalars().all()
            assert len(rows) == 2, "re-entering an instrument should leave a closed row plus the re-opened one"
            assert all(row.is_open is False for row in rows)
            # Each row carries the account's lifetime total at the moment it
            # was written, so they overlap -- summing them is exactly the bug.
            assert sum(float(row.realized_pnl) for row in rows) > expected

            body = client.get("/portfolio", headers=headers).json()
            assert body["open_position_count"] == 0
            assert body["total_realized_pnl"] == pytest.approx(expected, rel=1e-6)
        finally:
            await _cleanup(user_id, instrument_id)
