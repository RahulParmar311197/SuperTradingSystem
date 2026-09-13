"""A broker's own words must not be able to take down the order path.

Every string a broker hands back is diagnostic text of no promised
length: `UpstoxBroker._extract_error_message` falls back to the entire
response body, and a proxy 502 in front of the broker returns an HTML
page. Postgres `VARCHAR(n)` does not truncate -- it raises
`StringDataRightTruncation` and takes the transaction with it -- so
before `app/core/text.py`'s `clip`, a verbose broker made the write
itself the failure.

Measured on the two paths below:
  * a 789-character rejection reason -> `POST /orders` raised out of
    `persist_order` and left **no order row at all**: no status, no
    reason, no ORDER_REJECTED notification for an order the broker had
    refused. An identical retry failed identically.
  * a 996-character stop rejection -> the entry had already filled and
    was journaled MONITORING, the broker had refused the protective stop,
    and the write that failed was the notification telling the account
    holder their live position has no stop-loss.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.api.orders as orders_api
from app.brokers.base import OrderResult
from app.brokers.mock import MockBroker as _MockBrokerBase
from app.core.redis import account_halt_reason, resume_account
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification, NotificationType
from app.database.models.trading import Order, OrderStatus
from app.database.session import async_session_factory
from app.main import app
from tests.api.test_orders import _cleanup, _register_and_grant_live_trade

pytestmark = pytest.mark.asyncio

# What a proxy or CDN actually returns when the broker is unreachable --
# `_extract_error_message` hands the whole body back on a non-JSON reply.
HTML_502 = (
    "<html><head><title>502 Bad Gateway</title></head><body>"
    + "<p>The upstream server returned an error.</p>" * 20
    + "</body></html>"
)
LONG_STOP_REASON = "Order rejected: " + "the trigger price is outside the permitted band; " * 20
SHORT_REASON = "Insufficient margin"


class _VerboseRejectBroker(_MockBrokerBase):
    """Rejects everything, at length."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self._reason = reason

    async def place_order(self, request):
        return OrderResult(broker_order_id="", status=OrderStatus.REJECTED, rejection_reason=self._reason)


class _VerboseStopRejectBroker(_MockBrokerBase):
    """Takes the entry, refuses the protective stop, at length."""

    async def place_order(self, request):
        if request.order_type.value in ("SL", "SL_M"):
            return OrderResult(
                broker_order_id="", status=OrderStatus.REJECTED, rejection_reason=LONG_STOP_REASON
            )
        return await super().place_order(request)


async def _instrument(prefix: str) -> tuple[uuid.UUID, str]:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"{prefix}{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id, instrument.symbol


async def _place(broker, prefix: str):
    """Run one live order against `broker`, returning (response, user_id,
    instrument_id, symbol). The caller cleans up."""
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id, symbol = await _instrument(prefix)
        orders_api._STACKS[user_id] = orders_api._UserTradingStack(broker)
        response = client.post(
            "/orders",
            json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
            headers=headers,
        )
        return response, user_id, instrument_id, symbol


# --- a rejection nobody could read ----------------------------------------


async def test_a_verbose_broker_rejection_is_still_journaled(require_infra):
    assert len(HTML_502) > 500, "the fixture must exceed the column, or this proves nothing"
    response, user_id, instrument_id, _ = await _place(_VerboseRejectBroker(HTML_502), "VRB")
    try:
        # Before the fix this raised `StringDataRightTruncation` out of
        # `persist_order` instead of answering at all.
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "REJECTED"

        async with async_session_factory() as db:
            rows = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
            # The measured symptom was `[]` -- the rejection left no trace.
            assert len(rows) == 1
            assert rows[0].status is OrderStatus.REJECTED
            reason = rows[0].rejection_reason or ""
            assert len(reason) == 500
            assert reason.startswith("<html>")
            assert reason.endswith("…"), "a cut message must not read as the whole message"

            # Note on what is *not* asserted here: a broker rejection
            # produces no notification. `POST /orders` notifies on a
            # *risk-engine* rejection only (ORDER_REJECTED /
            # DAILY_LOSS_LIMIT, app/api/orders.py), and whether a
            # broker-side rejection deserves one too is a separate
            # question from this fix. The order row is the record either
            # way, and the row is what used to vanish.
            notifications = (
                await db.execute(select(Notification).where(Notification.user_id == user_id))
            ).scalars().all()
            assert [n.type for n in notifications] == []
    finally:
        orders_api._STACKS.pop(user_id, None)
        await _cleanup(user_id, instrument_id)


async def test_an_ordinary_rejection_reason_is_stored_word_for_word(require_infra):
    # Control: the clip must be invisible to every message that fits, or
    # it would be trading one bug for a quieter one.
    response, user_id, instrument_id, _ = await _place(_VerboseRejectBroker(SHORT_REASON), "SRB")
    try:
        assert response.status_code == 201, response.text
        async with async_session_factory() as db:
            rows = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
            assert [r.rejection_reason for r in rows] == [SHORT_REASON]
    finally:
        orders_api._STACKS.pop(user_id, None)
        await _cleanup(user_id, instrument_id)


# --- the alert that mattered most -----------------------------------------


async def test_a_verbose_stop_rejection_still_warns_the_account_holder(require_infra):
    assert len(LONG_STOP_REASON) > 500
    response, user_id, instrument_id, symbol = await _place(_VerboseStopRejectBroker(), "VSR")
    try:
        assert response.status_code == 201, response.text

        async with async_session_factory() as db:
            notifications = (
                await db.execute(select(Notification).where(Notification.user_id == user_id))
            ).scalars().all()
            # Measured before the fix: `[]`. The entry had filled, the
            # broker had refused the stop, and the write that failed was
            # this one -- so the position was live, unprotected, and
            # unannounced.
            unprotected = [
                n for n in notifications if n.type is NotificationType.RECONCILIATION_REQUIRED
            ]
            assert unprotected, "a position with no stop at the broker must reach the user"
            body = unprotected[0].body
            assert len(body) <= 1000
            assert symbol in body
            # `data` is a JSON column with no width, so the structured
            # half of the alert survives whole.
            assert unprotected[0].data["fate_unknown"] is False

            rows = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
            assert [r.status for r in rows] == [OrderStatus.MONITORING]

        assert await account_halt_reason(str(user_id)) is not None
    finally:
        await resume_account(str(user_id))
        orders_api._STACKS.pop(user_id, None)
        await _cleanup(user_id, instrument_id)


# --- the id that must not be clipped --------------------------------------


async def test_a_long_broker_id_reaches_both_journals(require_infra):
    """`orders.broker_order_id` and `positions.protective_order_id` hold
    the same value. They were declared 128 and 64, so an id in between was
    accepted by one and rejected by the other -- and the rejection landed
    *after* the protective stop was already resting at the venue.

    Clipping is not the fix here and never could be: a shortened order id
    is not a shorter name for the order, it is one that matches nothing at
    the broker. The column was widened instead.
    """
    from sqlalchemy import delete

    from app.database.models.trading import ExecutionMode, Position
    from app.database.models.users import User
    from app.trading.persistence import persist_position
    from app.trading.position_manager import PositionRecord

    broker_id = "ORD" + "X" * 97  # 100: fits String(128), not String(64)
    async with async_session_factory() as db:
        user = User(email=f"wid-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", name="Width")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        instrument_id, symbol = await _instrument("WID")
        try:
            position = PositionRecord(
                account_id=str(user.id), symbol=symbol, quantity=1, average_price=100.0
            )
            position.protective_order_id = broker_id
            row = await persist_position(
                db,
                user_id=user.id,
                instrument_id=instrument_id,
                execution_mode=ExecutionMode.LIVE,
                position=position,
                source_key="manual",
            )
            assert row.protective_order_id == broker_id
        finally:
            async with async_session_factory() as cleanup:
                await cleanup.execute(delete(Position).where(Position.user_id == user.id))
                await cleanup.execute(delete(User).where(User.id == user.id))
                await cleanup.execute(delete(Instrument).where(Instrument.id == instrument_id))
                await cleanup.commit()
