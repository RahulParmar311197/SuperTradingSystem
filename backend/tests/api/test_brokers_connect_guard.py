"""A broker connection that cannot trade must be refused, not stored.

`resolve_broker` picks the most recent ACTIVE `BrokerAccount` for every
order a user places, so an account it cannot build a working adapter from
does not degrade that user's trading -- it stops it. Measured before this
guard, both of these returned **201 ACTIVE**:

  * `{"broker": "DHAN", ...}` -- every later `POST /orders` came back 500
    with `NotImplementedError: TODO: implement using Dhan's market quote /
    LTP endpoint`, from the documented skeleton adapter.
  * `{"broker": "UPSTOX", "credentials": {"nope": "x"}}` -- every later
    `POST /orders` came back 500 with the `BrokerError` the resolver
    raises on purpose ("has no access_token stored"). That message is a
    good diagnosis; `_stack_for` simply had no handler, so nobody saw it.

Two layers here, because they cover different users: the connect guard
turns a new bad connection away, and the resolver refusal plus the 503
cover an account already stored before the guard existed.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.api.orders as orders_api
from app.core.encryption import encrypt_credentials
from app.database.models.instruments import Instrument, MarketType
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName
from app.database.session import async_session_factory
from app.main import app
from tests.api.test_orders import _cleanup, _register_and_grant_live_trade

pytestmark = pytest.mark.asyncio


async def _account_count(user_id: uuid.UUID) -> int:
    async with async_session_factory() as db:
        return len(
            (await db.execute(select(BrokerAccount).where(BrokerAccount.user_id == user_id)))
            .scalars()
            .all()
        )


async def _instrument() -> tuple[uuid.UUID, str]:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"BRK{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id, instrument.symbol


# --- the connect guard ----------------------------------------------------


async def test_connecting_dhan_is_refused(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = client.post(
                "/brokers/connect",
                json={"broker": "DHAN", "credentials": {"client_id": "c", "access_token": "t"}},
                headers=headers,
            )
            assert response.status_code == 501, response.text
            assert "not implemented" in response.json()["detail"].lower()
            # The refusal must not leave the row behind -- a stored ACTIVE
            # Dhan account is exactly the thing that breaks every order.
            assert await _account_count(user_id) == 0
        finally:
            await _cleanup(user_id, uuid.uuid4())


async def test_connecting_upstox_without_an_access_token_is_refused(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = client.post(
                "/brokers/connect",
                json={"broker": "UPSTOX", "credentials": {"nope": "x"}},
                headers=headers,
            )
            assert response.status_code == 422, response.text
            assert "access_token" in response.json()["detail"]
            assert await _account_count(user_id) == 0
        finally:
            await _cleanup(user_id, uuid.uuid4())


@pytest.mark.parametrize(
    ("broker", "credentials"),
    [
        ("UPSTOX", {"access_token": "tok"}),
        ("PAPER", {}),
    ],
)
async def test_a_usable_connection_is_still_accepted(require_infra, broker, credentials):
    # Control: the guard refuses what cannot trade, and nothing else.
    # PAPER carries no credentials at all by design.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = client.post(
                "/brokers/connect", json={"broker": broker, "credentials": credentials}, headers=headers
            )
            assert response.status_code == 201, response.text
            assert response.json()["status"] == "ACTIVE"
            assert await _account_count(user_id) == 1
        finally:
            await _cleanup(user_id, uuid.uuid4())


# --- accounts already stored ----------------------------------------------


async def _stored_account_then_order(user_id, headers, client, account: BrokerAccount):
    async with async_session_factory() as db:
        db.add(account)
        await db.commit()
    instrument_id, symbol = await _instrument()
    orders_api._STACKS.pop(user_id, None)
    response = client.post(
        "/orders",
        json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
        headers=headers,
    )
    return response, instrument_id


async def test_an_already_stored_dhan_account_gets_503_not_500(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id = None
        try:
            response, instrument_id = await _stored_account_then_order(
                user_id,
                headers,
                client,
                BrokerAccount(
                    user_id=user_id,
                    broker=BrokerName.DHAN,
                    encrypted_credentials=encrypt_credentials({"client_id": "c", "access_token": "t"}),
                    status=BrokerAccountStatus.ACTIVE,
                ),
            )
            # Before the fix this raised NotImplementedError out of the
            # skeleton's `get_quote`, several frames below the endpoint.
            assert response.status_code == 503, response.text
            detail = response.json()["detail"]
            assert "Dhan" in detail and "not implemented" in detail

            # Every endpoint that builds a trading stack, not just /orders.
            orders_api._STACKS.pop(user_id, None)
            assert client.get("/positions", headers=headers).status_code == 503
        finally:
            orders_api._STACKS.pop(user_id, None)
            await _cleanup(user_id, instrument_id or uuid.uuid4())


async def test_the_resolvers_own_diagnosis_reaches_the_client(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id = None
        try:
            response, instrument_id = await _stored_account_then_order(
                user_id,
                headers,
                client,
                BrokerAccount(
                    user_id=user_id,
                    broker=BrokerName.UPSTOX,
                    encrypted_credentials=encrypt_credentials({"nope": "x"}),
                    status=BrokerAccountStatus.ACTIVE,
                ),
            )
            assert response.status_code == 503, response.text
            # The point of the fix: the resolver already wrote this
            # sentence, naming the account. It used to die in a traceback.
            assert "no access_token stored" in response.json()["detail"]
        finally:
            orders_api._STACKS.pop(user_id, None)
            await _cleanup(user_id, instrument_id or uuid.uuid4())
