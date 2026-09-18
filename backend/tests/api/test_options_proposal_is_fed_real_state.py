"""Every counter the options risk engine reads must come from the real stack.

This pins the bug class this codebase has hit three times: a field on a
`RiskProposal` left at its dataclass default, so the check that reads it
records a **pass** in the `RiskEvent` audit row while measuring nothing.

    round 121  `liquidity_acceptable` had no writer on the equity path
    round 134  three options quote checks were recorded without being evaluated
    round 141  `market_data_age_seconds=0.0` made `market_data_fresh` unfailable
               on the autonomous path

Each was found by reading the code. Nothing would have caught the next one,
because a defaulted field produces a *passing* check and a green suite.

The approach here is per-field and behavioural rather than structural: for
each counter, put the stack in a state where that counter **alone** must
cause a rejection, and assert the rejection names it. A field silently
reading its default cannot produce that rejection, so each test fails
loudly the moment the wiring is cut — which is exactly what did not happen
the three times above.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.api.orders as orders_module
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

from datetime import date, timedelta


async def _register_with_live_trade(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"optstate-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Opt State"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
    assert r.status_code == 200, r.text
    return headers, user_id


async def _two_legs(prefix: str) -> tuple[Instrument, Instrument]:
    expiry = date.today() + timedelta(days=7)
    async with async_session_factory() as db:
        legs = [
            Instrument(
                symbol=f"{prefix}{strike}CE", exchange="NSE", market=MarketType.OPTIONS,
                instrument_type="OPTION", underlying="NIFTY", expiry=expiry, strike=float(strike),
                option_type=OptionType.CALL, lot_size=50,
            )
            for strike in (25000, 25200)
        ]
        db.add_all(legs)
        await db.commit()
        for leg in legs:
            await db.refresh(leg)
        return legs[0], legs[1]


async def _cleanup(user_id: uuid.UUID, instruments: list[Instrument]) -> None:
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        for order_id in order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
        for model in (Order, Trade, Position, RiskEvent, Notification, AuditLog, UserSession):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id.in_([i.id for i in instruments])))
        await db.commit()
    orders_module._STACKS.pop(user_id, None)


def _payload(long_leg: Instrument, short_leg: Instrument) -> dict:
    return {
        "strategy_name": "bull_call_spread",
        "legs": [
            {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 120.0},
            {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 50.0},
        ],
    }


# Each entry: the stack attribute to poison, the value, the limit to tighten
# so that value alone is disqualifying, and the check that must fail.
POISONS = [
    ("trades_today", 99, {"max_trades_per_day": 1}, "max_trades_per_day"),
    ("daily_pnl", -50_000.0, {"max_daily_loss_pct": 1.0}, "daily_loss_limit"),
    ("weekly_pnl", -50_000.0, {"max_weekly_loss_pct": 1.0}, "weekly_loss_limit"),
    ("repeated_rejections", 99, {"max_repeated_rejections": 2}, "no_repeated_rejections"),
]


@pytest.mark.parametrize(
    ("attribute", "value", "limit_overrides", "failing_check"),
    POISONS,
    ids=[p[0] for p in POISONS],
)
async def test_the_options_proposal_reads_this_counter_from_the_stack(
    require_infra, attribute, value, limit_overrides, failing_check
):
    """Behavioural proof, one per counter.

    The stack is put in a state where this counter alone disqualifies the
    order. If `POST /options/execute` ever stopped passing it — the way
    three earlier rounds found other fields not being passed — the engine
    would read the dataclass default, approve, and record the check as
    having passed.
    """
    prefix = f"PS{uuid.uuid4().hex[:5].upper()}"
    long_leg, short_leg = await _two_legs(prefix)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _register_with_live_trade(client)
        try:
            # Build the stack, then poison exactly one counter on it.
            async with async_session_factory() as db:
                user = await db.get(User, user_id)
                stack = await orders_module._stack_for(user, db)
            setattr(stack, attribute, value)
            for name, limit_value in limit_overrides.items():
                setattr(stack.risk_engine.limits, name, limit_value)

            r = client.post("/options/execute", json=_payload(long_leg, short_leg), headers=headers)

            assert r.status_code == 403, f"{attribute}={value} must block the order: {r.text}"
            async with async_session_factory() as db:
                events = (
                    await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))
                ).scalars().all()
            assert len(events) == 1, events
            assert events[0].checks.get(failing_check) is False, events[0].checks
        finally:
            await _cleanup(user_id, [long_leg, short_leg])


async def test_an_unpoisoned_stack_still_executes(require_infra):
    """Control, and the one that stops every test above passing for the
    wrong reason. Same instruments, same strategy, default limits: if this
    failed too, the four proofs would only be showing that this endpoint
    rejects everything."""
    prefix = f"PC{uuid.uuid4().hex[:5].upper()}"
    long_leg, short_leg = await _two_legs(prefix)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _register_with_live_trade(client)
        try:
            r = client.post("/options/execute", json=_payload(long_leg, short_leg), headers=headers)
            assert r.status_code == 201, r.text
            async with async_session_factory() as db:
                events = (
                    await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))
                ).scalars().all()
            assert len(events) == 1
            for _attribute, _value, _limits, check in POISONS:
                assert events[0].checks.get(check) is True, events[0].checks
        finally:
            await _cleanup(user_id, [long_leg, short_leg])


async def test_the_options_proposal_counts_open_positions_from_the_manager(require_infra):
    """Behavioural proof for the one field the parametrised cases above
    could not reach, and injection is why it is here.

    `open_positions` is not a stack counter — it is `len(...)` over the
    shared `PositionManager` — so poisoning an attribute could not touch
    it, and defaulting it to 0 in the proposal left the whole suite green.
    That is precisely the shape of the three rounds this file exists for.

    The position is deliberately tiny (1 unit at 1.0) so it cannot trip
    `exposure_limit` first and mask which check actually failed.
    """
    from app.database.models.strategy import Direction

    prefix = f"PO{uuid.uuid4().hex[:5].upper()}"
    long_leg, short_leg = await _two_legs(prefix)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _register_with_live_trade(client)
        try:
            async with async_session_factory() as db:
                user = await db.get(User, user_id)
                stack = await orders_module._stack_for(user, db)
            stack.position_manager.apply_fill(
                str(user_id), f"{prefix}UNRELATED", Direction.LONG, quantity=1.0, price=1.0
            )
            assert len(stack.position_manager.open_positions(str(user_id))) == 1
            stack.risk_engine.limits.max_open_positions = 1

            r = client.post("/options/execute", json=_payload(long_leg, short_leg), headers=headers)

            assert r.status_code == 403, r.text
            async with async_session_factory() as db:
                events = (
                    await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))
                ).scalars().all()
            assert len(events) == 1, events
            assert events[0].checks.get("max_open_positions") is False, events[0].checks
        finally:
            await _cleanup(user_id, [long_leg, short_leg])
