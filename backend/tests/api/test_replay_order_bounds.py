"""Every number on `POST /replay/{id}/order` was unvalidated.

`ReplayOrderRequest` was three bare fields -- `action: str`, `quantity:
float | None`, `price: float | None` -- and the route spent them as
`engine.buy(payload.quantity or 1)` and `engine.set_stop(payload.price)`.
Six things measured against the live endpoint before the fix:

    {"action": "buy", "quantity": 0}      -> 200, a 1-unit LONG opened
                                             (`or 1`: the client asked for
                                             nothing and got a position)
    {"action": "buy", "quantity": -5}     -> 200, a LONG of -5 units. Entry
                                             100, exit 90: balance goes UP
                                             50 and the session reports
                                             win_rate 1.0, best_trade 50.0
                                             for a trade that lost.
    {"action": "buy", "quantity": 1e308}  -> 500  NumericValueOutOfRangeError
    {"action": "buy", "quantity": NaN}    -> 200, and then the close 500s
    {"action": "set_stop"}                -> 200, and the stop is CLEARED
    {"action": "close", "price": -1e6}    -> 200, balance -900100.0

The stop one is the one that costs a user something real. A long with a
stop at 99.5, stepped five bars through a low of 96, closes at 99.5 for a
0.5 loss. The same long after one no-price `set_stop` is still open and
unprotected at the end of those same five bars -- and the call that
disarmed it answered 200. That is a replay session, so the money is
imaginary; what is not imaginary is that this is the endpoint blueprint
§43 trains a user's stop discipline on.

The bounds are the ones `app/api/orders.py` already established for the
live path (round 101): `gt=0, lt=1e12`, which is what a `Numeric(18, 6)`
column holds, and which rejects `NaN` and `inf` for free.

A per-field bound is NOT sufficient on its own, which is how the second
half of this round was found: P&L is a *product*. A 1e11-unit position
fits the `quantity` column, and closing it 100 points from its entry
books 1e13, which overflowed `replay_orders.pnl` and 500'd the request
with the trade already closed in the engine's memory. `ReplayEngine` now
refuses such a fill where the product is formed -- and refuses it at
`set_stop`/`set_target` time too, so a stop firing from `advance()`, which
has no request to reject, can never be the thing that overflows.
"""

import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.replay import ReplayOrder, ReplaySession
from app.database.models.risk import AuditLog
from app.database.models.strategy import Direction
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.replay.engine import ReplayEngine, ReplayError
from app.smc.types import Candle

# Candle 0 closes at 100 -- every entry below is at 100. Bars 1-5 have
# lows 100, 100, 97, 96, 96, so a stop at 99.5 fires on bar 3.
_UNIT = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
    (104, 130, 104, 128),
]


def _candles() -> list[Candle]:
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    return [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(_UNIT * 2)]


async def _make_instrument() -> uuid.UUID:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"RQB{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
        )
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", _candles())
        await db.commit()
    return instrument_id


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"replaybounds-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "RB"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    """Child rows first: replay_orders before replay_sessions, and
    audit_logs/sessions before the user."""
    async with async_session_factory() as db:
        session_ids = (
            await db.execute(select(ReplaySession.id).where(ReplaySession.instrument_id == instrument_id))
        ).scalars().all()
        for session_id in session_ids:
            await db.execute(delete(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))
        await db.execute(delete(ReplaySession).where(ReplaySession.instrument_id == instrument_id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


class _Session:
    """One replay session and the three calls these tests make on it."""

    def __init__(self, client: TestClient, headers: dict, instrument_id: uuid.UUID) -> None:
        self._client, self._headers = client, headers
        r = client.post("/replay", json={"instrument_id": str(instrument_id), "timeframe": "15m"}, headers=headers)
        assert r.status_code == 200, r.text
        self.id = r.json()["session_id"]

    def order(self, **body):
        return self._client.post(f"/replay/{self.id}/order", json=body, headers=self._headers)

    def step(self, steps: int):
        return self._client.post(f"/replay/{self.id}/step?steps={steps}", headers=self._headers)

    def state(self) -> dict:
        r = self._client.get(f"/replay/{self.id}", headers=self._headers)
        assert r.status_code == 200, r.text
        return r.json()


# --- the quantity field ---------------------------------------------------

# Every one of these was measured as the stated wrong answer before the bound.
BAD_QUANTITIES = [
    (0, "opened a 1-unit position -- `payload.quantity or 1`"),
    (-5, "opened a LONG of -5 units whose P&L is sign-inverted"),
    (1e308, "500 NumericValueOutOfRangeError"),
    (float("inf"), "500 NumericValueOutOfRangeError"),
    (float("nan"), "accepted, then 500 on the close"),
    (1e12, "at the Numeric(18, 6) ceiling"),
]


@pytest.mark.parametrize("quantity,before", BAD_QUANTITIES, ids=[str(q) for q, _ in BAD_QUANTITIES])
async def test_a_quantity_that_is_not_a_quantity_is_refused_and_opens_nothing(quantity, before, require_infra):
    """Behavioural proof. The 422 is half of it; the other half is that no
    position exists afterwards -- `quantity=0` used to answer 200 with a
    1-unit LONG open, which a status-code-only assertion would miss."""
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            r = session.order(action="buy", quantity=quantity)
            assert r.status_code == 422, f"quantity={quantity} {before}; got {r.status_code}: {r.text[:200]}"
            assert any("quantity" in str(d.get("loc", "")) for d in r.json()["detail"]), r.text[:300]
            assert session.state()["open_trade"] is None, "the refused order must not have opened a position"
        finally:
            await _cleanup(user_id, instrument_id)


async def test_an_omitted_quantity_still_means_one_unit(require_infra):
    """Control. `payload.quantity or 1` became
    `payload.quantity if payload.quantity is not None else 1`; the
    documented default must survive that."""
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            r = session.order(action="buy")
            assert r.status_code == 200, r.text
            assert r.json()["open_trade"]["quantity"] == 1, r.json()["open_trade"]
        finally:
            await _cleanup(user_id, instrument_id)


# --- the price field ------------------------------------------------------

BAD_PRICES = [
    ("close", -1e6, "200, and a balance of -900100.0"),
    ("close", 0, "a fill at zero"),
    ("close", 1e308, "500 NumericValueOutOfRangeError"),
    ("close", float("nan"), "a NaN exit price"),
    ("set_stop", -1, "a stop below zero"),
    ("set_stop", 1e308, "500 NumericValueOutOfRangeError"),
    ("set_target", float("inf"), "500"),
]


@pytest.mark.parametrize("action,price,before", BAD_PRICES, ids=[f"{a}={p}" for a, p, _ in BAD_PRICES])
async def test_a_price_that_is_not_a_price_is_refused(action, price, before, require_infra):
    """Behavioural proof. The position must be untouched afterwards: the
    negative-price close used to go through and take the account to
    -900100.0."""
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            assert session.order(action="buy", quantity=1).status_code == 200
            r = session.order(action=action, price=price)
            assert r.status_code == 422, f"{action} at {price} {before}; got {r.status_code}: {r.text[:200]}"

            state = session.state()
            assert state["balance"] == 100000.0, state
            assert state["open_trade"] == {
                "direction": "LONG", "entry_price": 100.0, "quantity": 1.0, "stop": None, "target": None
            }, state
        finally:
            await _cleanup(user_id, instrument_id)


# --- the action that IS a price ------------------------------------------


@pytest.mark.parametrize("action", ["set_stop", "set_target"])
async def test_setting_a_level_with_no_price_is_refused_and_leaves_the_level_alone(action, require_infra):
    """Behavioural proof, and the heart of this round. `engine.set_stop`
    assigns whatever it is handed, so a no-price call used to set the
    level to `None` and answer 200. Asserting only the 422 would not
    catch a version that refused the request *after* clearing it."""
    instrument_id = await _make_instrument()
    field = "stop" if action == "set_stop" else "target"
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            assert session.order(action="buy", quantity=1).status_code == 200
            assert session.order(action=action, price=95.0 if field == "stop" else 120.0).status_code == 200

            r = session.order(action=action)
            assert r.status_code == 422, r.text[:200]
            assert action in r.text, r.text[:300]

            expected = 95.0 if field == "stop" else 120.0
            assert session.state()["open_trade"][field] == expected, (
                f"the refused call must not have cleared the {field}: {session.state()['open_trade']}"
            )
        finally:
            await _cleanup(user_id, instrument_id)


async def test_the_stop_still_fires_after_a_refused_no_price_set_stop(require_infra):
    """Behavioural proof of the consequence, not just the mechanism.

    Measured before the fix on exactly these five bars: with the stop
    intact the trade closes at 99.5 for a 0.5 loss; after one no-price
    `set_stop` the same trade is still open at the end of them, having run
    through a low of 96 with nothing protecting it.
    """
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            session.order(action="buy", quantity=1)
            session.order(action="set_stop", price=99.5)
            assert session.order(action="set_stop").status_code == 422

            session.step(5)
            state = session.state()
            assert state["open_trade"] is None, f"the stop must still have fired: {state}"
            assert state["balance"] == pytest.approx(99999.5), state
        finally:
            await _cleanup(user_id, instrument_id)


# --- the product, which no per-field bound reaches ------------------------


async def test_a_fill_the_journal_cannot_hold_is_refused_instead_of_500ing(require_infra):
    """Behavioural proof at the second layer. 1e11 units passes every
    field bound -- it fits the `quantity` column -- and closing it 100
    points away books 1e13, which used to reach Postgres and 500 with the
    trade already closed in the engine. The session must still be usable
    afterwards, which is the part a bare status-code assertion misses."""
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            assert session.order(action="buy", quantity=1e11).status_code == 200

            r = session.order(action="close", price=200.0)
            assert r.status_code == 409, f"expected a refusal, got {r.status_code}: {r.text[:200]}"
            assert "P&L" in r.json()["detail"], r.json()

            # Still open, still tradable: the refusal happened before the
            # engine mutated anything.
            assert session.state()["open_trade"]["quantity"] == 1e11, session.state()
            r = session.order(action="close", price=100.5)
            assert r.status_code == 200, r.text
            assert r.json()["balance"] == pytest.approx(100000 + 0.5 * 1e11), r.json()["balance"]
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_stop_the_journal_cannot_hold_is_refused_when_it_is_set(require_infra):
    """Behavioural proof for the path `advance()` takes. A stop fires from
    inside stepping, where there is no request to answer 422 to, so the
    level has to be refused when it is placed -- otherwise the overflow
    lands in the middle of a step."""
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            assert session.order(action="buy", quantity=1e11).status_code == 200
            r = session.order(action="set_stop", price=0.01)
            assert r.status_code == 409, f"expected a refusal, got {r.status_code}: {r.text[:200]}"
            assert session.state()["open_trade"]["stop"] is None, session.state()
        finally:
            await _cleanup(user_id, instrument_id)


# --- what must still work -------------------------------------------------


async def test_an_ordinary_managed_trade_is_unchanged(require_infra):
    """Control, with every number pinned and derived from the fixture: buy
    5 at the candle-0 close of 100, stop 95, target 120, close at 110.
    P&L = (110-100)*5 = 50; R = (110-100)/(100-95) = 2.0. A bound that
    quietly altered an input would pass every 422 assertion above."""
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            assert session.order(action="buy", quantity=5).status_code == 200
            assert session.order(action="set_stop", price=95.0).status_code == 200
            assert session.order(action="set_target", price=120.0).status_code == 200
            r = session.order(action="close", price=110.0)
            assert r.status_code == 200, r.text

            body = r.json()
            assert body["balance"] == pytest.approx(100050.0), body
            statistics = body["statistics"]
            assert statistics["trades"] == 1, statistics
            assert statistics["net_pnl"] == pytest.approx(50.0), statistics
            assert statistics["average_r"] == pytest.approx(2.0), statistics
            assert statistics["win_rate"] == pytest.approx(1.0), statistics
        finally:
            await _cleanup(user_id, instrument_id)


ACCEPTED = [
    ("buy", "quantity", 1e9),
    ("buy", "quantity", 0.001),          # a fractional lot
    ("set_stop", "price", 0.0001),       # a stop far below, on a 1-unit trade
    ("set_target", "price", 1e9),
    ("close", "price", 1e6),
]


@pytest.mark.parametrize("action,field,value", ACCEPTED, ids=[f"{a}.{f}={v}" for a, f, v in ACCEPTED])
async def test_a_legitimate_extreme_is_still_accepted(action, field, value, require_infra):
    """Control against over-tightening, in the other direction. Each of
    these is arithmetic the session can carry out and journal, so refusing
    it would be the endpoint declining work it can do."""
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            if action != "buy":
                assert session.order(action="buy", quantity=1).status_code == 200
            body = {"action": action, field: value}
            r = session.order(**body)
            assert r.status_code == 200, f"{action} {field}={value}: {r.status_code} {r.text[:200]}"
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_close_outside_the_candles_traded_range_is_deliberately_still_accepted(require_infra):
    """Control, and a deliberate non-fix.

    A manual replay session is a simulation the user drives, and round 105
    settled the same question for `POST /paper/{id}/candle`: "the bounds
    assert only that the numbers are prices at all". Candle 0 traded
    99-100; a close at 1e6 is nowhere near it and is still the user's to
    ask for. This test fails if a traded-range check is ever added here
    without a measured reason to add one.
    """
    instrument_id = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            session = _Session(client, headers, instrument_id)
            assert session.order(action="buy", quantity=1).status_code == 200
            r = session.order(action="close", price=1e6)
            assert r.status_code == 200, r.text
            assert r.json()["balance"] == pytest.approx(100000 + (1e6 - 100)), r.json()["balance"]
        finally:
            await _cleanup(user_id, instrument_id)


# --- the engine on its own ------------------------------------------------


def test_advance_still_fires_an_ordinary_stop():
    """Control at the engine layer, with no HTTP and no database. The new
    guard sits in `set_stop`, which is on the path every managed trade
    takes -- a guard that refused ordinary levels would break stepping,
    and the endpoint tests above would still pass if it merely made
    `set_stop` a no-op."""
    engine = ReplayEngine(_candles())
    engine.buy(2)
    engine.set_stop(99.5)
    assert engine.open_trade.stop == 99.5
    engine.advance(5)
    assert engine.open_trade is None, "the stop must have fired"
    assert engine.closed_trades[-1].exit_price == pytest.approx(99.5)
    assert engine.balance == pytest.approx(100000 - 1.0)


def test_the_engine_guard_measures_the_trade_it_is_given_not_a_fixed_price():
    """Proof at the engine layer that the guard is about the *product*.

    The same exit price is fine on a small position and refused on a huge
    one; a guard that simply capped the price would get the first of these
    wrong, and one that capped the quantity would get the second wrong.
    """
    small = ReplayEngine(_candles())
    small.buy(1)
    small.close(price=1e6)  # a 999900 P&L, journallable
    assert small.balance == pytest.approx(100000 + (1e6 - 100))

    huge = ReplayEngine(_candles())
    huge.buy(1e11)
    with pytest.raises(ReplayError, match="P&L"):
        huge.close(price=1e6)
    assert huge.open_trade is not None, "a refused close must not have closed the trade"
    assert huge.open_trade.exit_price is None
    assert huge.balance == 100000.0


def test_the_guard_counts_the_running_balance_too():
    """Proof that the second half of the guard is live. A single P&L can
    fit `Numeric(18, 6)` while the balance it produces does not -- the
    session row has the same column."""
    engine = ReplayEngine(_candles(), starting_balance=9e11)
    engine.buy(1e10)
    pnl_alone = engine._pnl_at(150.0)
    assert abs(pnl_alone) < 1e12, "this fixture only tests the balance half if the P&L itself fits"
    with pytest.raises(ReplayError, match="P&L"):
        engine.close(price=150.0)


def test_a_short_books_its_loss_as_a_loss():
    """Proof that `_pnl_at` carries the sign into what is recorded.

    Closing the short *above* its entry on purpose: price up is a SHORT's
    losing direction, so the balance must fall. An earlier version of this
    test closed at 99.5, a winner, and injecting `abs()` into `_pnl_at`
    left it green -- both spellings give +5e10 there. The guard itself is
    sign-blind by construction (it compares `abs(pnl)`), so the sign can
    only be proved by the number that comes out.
    """
    engine = ReplayEngine(_candles())
    engine.sell(1e11)
    assert engine.open_trade.direction == Direction.SHORT
    with pytest.raises(ReplayError, match="P&L"):
        engine.close(price=1000.0)
    engine.close(price=100.5)
    assert engine.closed_trades[-1].pnl == pytest.approx(-0.5 * 1e11)
    assert engine.balance == pytest.approx(100000 - 0.5 * 1e11)


def test_nothing_in_the_engine_produces_a_non_finite_number():
    """Control. The route's bounds keep NaN and inf out, but the guard
    below is what would have to catch one that arrived another way:
    `abs(nan) >= 1e12` is False, so a NaN would sail through it. This
    records that the field bounds are the only thing standing there."""
    engine = ReplayEngine(_candles())
    engine.buy(1)
    engine.close(price=110.0)
    assert math.isfinite(engine.balance)
    assert math.isfinite(engine.closed_trades[-1].pnl)
