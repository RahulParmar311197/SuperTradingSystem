"""A lot is indivisible, and `POST /options/execute` let a fraction through.

`ExecuteOptionLegRequest.quantity` is documented in its own comment as
"number of lots". Rounds 102/140 bounded the field's magnitude and sign
-- a negative premium used to be approved and executed -- but neither
bounded its INTEGRALITY, and the field was a `float`.

Measured through the real route, on two contracts registered with
`lot_size=50`:

    quantity 0.5  lots -> 201, position  25.0 contracts
    quantity 0.33 lots -> 201, position  41.5 contracts

41.5 option contracts is not a position any exchange can represent. It
cleared every risk gate, was priced, went to the broker and was
journaled. A real broker would reject or silently truncate the order --
truncation being the worse half, because the app's book would then
disagree with the broker's about a position it believes it holds, which
is the same divergence class as round 169's phantom short.

ONE LAYER, deliberately. `execute_options_strategy` is the only entry to
this path and it takes `ExecuteOptionsStrategyRequest`, so the request
model is the single gate rather than the outer half of a pair -- there is
no second layer here that could stand in for it, and inventing one would
put a bound where no failure was measured.
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.api.admin as admin_module
from app.database.models.trading import Position
from app.database.session import async_session_factory
from app.main import app

from tests.api.test_admin_option_chain import (
    _StubChainProvider,
    _chain_payload,
    _cleanup,
    _make_admin,
    _register,
    _seed_instruments,
)


async def _positions(user_id: uuid.UUID) -> list[float]:
    async with async_session_factory() as db:
        return [
            float(p.quantity)
            for p in (
                await db.execute(select(Position).where(Position.user_id == user_id))
            ).scalars().all()
        ]


def _legs(low, high, quantity):
    return [
        {"symbol": low.symbol, "direction": "LONG", "quantity": quantity, "premium": 120.0},
        {"symbol": high.symbol, "direction": "SHORT", "quantity": quantity, "premium": 50.0},
    ]


async def _execute(client, low, high, quantity, headers):
    return client.post(
        "/options/execute",
        json={"strategy_name": "bull_call_spread", "legs": _legs(low, high, quantity)},
        headers=headers,
    )


class _Harness:
    """Ingest a real chain so the three options gates are live, then let
    each test drive `POST /options/execute` against it."""

    def __init__(self, client, low, high, trader_headers, trader_id):
        self.client = client
        self.low = low
        self.high = high
        self.headers = trader_headers
        self.user_id = trader_id


async def _setup(client, monkeypatch, label):
    prefix = f"LI{uuid.uuid4().hex[:5].upper()}"
    underlying, low, high = await _seed_instruments(prefix)
    stub = _StubChainProvider(_chain_payload(low.broker_instrument_key, high.broker_instrument_key))
    monkeypatch.setattr(admin_module, "market_data_provider_or_reason", lambda: (stub, ""))
    admin_headers, admin_id = await _make_admin(client, f"{label}admin")
    trader_token, trader_id = await _register(client, f"{label}trader")
    trader_headers = {"Authorization": f"Bearer {trader_token}"}
    client.post(
        "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=trader_headers
    )
    r = client.post(
        "/admin/option-chain",
        json={"underlying": underlying.symbol, "expiry": low.expiry.isoformat()},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    return _Harness(client, low, high, trader_headers, trader_id), [admin_id, trader_id], [underlying, low, high]


# --- the finding ----------------------------------------------------------


async def test_half_a_lot_is_refused(require_infra, monkeypatch):
    """The headline. 0.5 lots used to answer 201 and open 25 contracts."""
    with TestClient(app) as client:
        h, user_ids, instruments = await _setup(client, monkeypatch, "halflot")
        try:
            r = await _execute(client, h.low, h.high, 0.5, h.headers)
            assert r.status_code == 422, r.text
            assert await _positions(h.user_id) == [], "a refused request must open nothing"
        finally:
            await _cleanup(user_ids, instruments)


async def test_a_third_of_a_lot_is_refused(require_infra, monkeypatch):
    """0.33 lots x lot_size 50 is 16.5 contracts -- a half contract, which
    is the shape that makes this unrepresentable rather than merely odd."""
    with TestClient(app) as client:
        h, user_ids, instruments = await _setup(client, monkeypatch, "thirdlot")
        try:
            r = await _execute(client, h.low, h.high, 0.33, h.headers)
            assert r.status_code == 422, r.text
            assert await _positions(h.user_id) == []
        finally:
            await _cleanup(user_ids, instruments)


async def test_one_whole_lot_still_executes(require_infra, monkeypatch):
    """Non-vacuity. A bound that rejected everything would pass both tests
    above, so this pins the ordinary case AND the arithmetic: one lot of a
    `lot_size=50` contract is 50 contracts, not 1."""
    with TestClient(app) as client:
        h, user_ids, instruments = await _setup(client, monkeypatch, "onelot")
        try:
            r = await _execute(client, h.low, h.high, 1, h.headers)
            assert r.status_code == 201, r.text
            assert sorted(await _positions(h.user_id)) == [-50.0, 50.0], await _positions(h.user_id)
        finally:
            await _cleanup(user_ids, instruments)


async def test_two_whole_lots_still_execute(require_infra, monkeypatch):
    """The over-fix in the other direction: a bound pinned at exactly one
    lot would pass every test above. Two lots is 100 contracts."""
    with TestClient(app) as client:
        h, user_ids, instruments = await _setup(client, monkeypatch, "twolots")
        try:
            r = await _execute(client, h.low, h.high, 2, h.headers)
            assert r.status_code == 201, r.text
            assert sorted(await _positions(h.user_id)) == [-100.0, 100.0], await _positions(h.user_id)
        finally:
            await _cleanup(user_ids, instruments)


async def test_an_integral_float_is_still_accepted(require_infra, monkeypatch):
    """`1.0` is a float and is a whole lot. This repo's own tests send it
    (tests/api/test_options_execute.py), and so will any JSON client that
    serialises numbers as floats -- so the fix must reject a FRACTION, not
    reject the float type."""
    with TestClient(app) as client:
        h, user_ids, instruments = await _setup(client, monkeypatch, "floatlot")
        try:
            r = await _execute(client, h.low, h.high, 1.0, h.headers)
            assert r.status_code == 201, r.text
            assert sorted(await _positions(h.user_id)) == [-50.0, 50.0]
        finally:
            await _cleanup(user_ids, instruments)
