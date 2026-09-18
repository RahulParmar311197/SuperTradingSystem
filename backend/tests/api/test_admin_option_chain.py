"""`POST /admin/option-chain` — the first writer `option_snapshots` ever had.

The three options-specific gates on `POST /options/execute` (liquidity,
premium deviation against the real mid, quote staleness) all read
`option_snapshots`, and nothing in `app/` wrote a row. An earlier round
made the endpoint record `None` for each rather than let the audit row
claim a check that never ran — honest, and inert. These tests drive the
real route and then assert the gates come alive because of it.

Wiring, not logic, has been the uncovered half in four of the last six
rounds, so the last test here is deliberately end to end: ingest through
the HTTP route, then execute through the HTTP route, and read what landed
in the audit row.
"""

import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.api.admin as admin_module
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification
from app.database.models.options import OptionChainSnapshot, OptionContract, OptionSnapshot
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserRole, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.providers.upstox import parse_option_chain

LOW_KEY_SUFFIX = "C25000"
HIGH_KEY_SUFFIX = "C25200"


def _chain_payload(low_key: str, high_key: str) -> dict:
    """Two strikes of the same expiry, calls only.

    A bull call spread needs two calls; a call and a put at one strike is
    an unbounded position and the exposure gate rejects it before any
    quote check is reached, which would hide exactly what these tests are
    trying to show.
    """

    def _row(strike: float, key: str, bid: float, ask: float) -> dict:
        return {
            "strike_price": strike,
            "underlying_spot_price": 25123.45,
            "call_options": {
                "instrument_key": key,
                "market_data": {
                    "ltp": (bid + ask) / 2,
                    "volume": 50000,
                    "oi": 90000,
                    "bid_price": bid,
                    "ask_price": ask,
                },
                "option_greeks": {"iv": 14.2, "delta": 0.55, "gamma": 0.001, "theta": -8.4, "vega": 12.1},
            },
        }

    return {
        "status": "success",
        "data": [_row(25000.0, low_key, 119.0, 121.0), _row(25200.0, high_key, 49.0, 51.0)],
    }


class _StubChainProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, date]] = []

    async def get_option_chain(self, instrument_key: str, expiry: date):
        self.calls.append((instrument_key, expiry))
        return parse_option_chain(self.payload)


async def _register(client: TestClient, label: str) -> tuple[str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]
    from app.auth.security import TokenType, decode_token

    return token, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _make_admin(client: TestClient, label: str) -> tuple[dict, uuid.UUID]:
    token, user_id = await _register(client, label)
    async with async_session_factory() as db:
        admin_user = await db.get(User, user_id)
        admin_user.role = UserRole.ADMIN
        await db.commit()
    return {"Authorization": f"Bearer {token}"}, user_id


async def _seed_instruments(prefix: str) -> tuple[Instrument, Instrument, Instrument]:
    expiry = date.today() + timedelta(days=7)
    async with async_session_factory() as db:
        underlying = Instrument(
            symbol=f"{prefix}IDX",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="INDEX",
            broker_instrument_key=f"NSE_INDEX|{prefix}",
            lot_size=1,
        )
        low = Instrument(
            symbol=f"{prefix}25000CE",
            exchange="NSE",
            market=MarketType.OPTIONS,
            instrument_type="OPTION",
            underlying=f"{prefix}IDX",
            expiry=expiry,
            strike=25000.0,
            option_type=OptionType.CALL,
            broker_instrument_key=f"NSE_FO|{prefix}{LOW_KEY_SUFFIX}",
            lot_size=50,
        )
        high = Instrument(
            symbol=f"{prefix}25200CE",
            exchange="NSE",
            market=MarketType.OPTIONS,
            instrument_type="OPTION",
            underlying=f"{prefix}IDX",
            expiry=expiry,
            strike=25200.0,
            option_type=OptionType.CALL,
            broker_instrument_key=f"NSE_FO|{prefix}{HIGH_KEY_SUFFIX}",
            lot_size=50,
        )
        db.add_all([underlying, low, high])
        await db.commit()
        for row in (underlying, low, high):
            await db.refresh(row)
        return underlying, low, high


async def _cleanup(user_ids: list[uuid.UUID], instruments: list[Instrument]) -> None:
    async with async_session_factory() as db:
        ids = [i.id for i in instruments]
        # By chain, not by instrument: a chain may also carry contracts for
        # instruments this test did not create, and deleting it out from
        # under one of those is an FK violation -- a failing teardown on a
        # test whose assertions passed.
        chain_ids = set(
            (
                await db.execute(select(OptionContract.chain_id).where(OptionContract.instrument_id.in_(ids)))
            ).scalars().all()
        )
        if chain_ids:
            contract_ids = (
                await db.execute(select(OptionContract.id).where(OptionContract.chain_id.in_(chain_ids)))
            ).scalars().all()
            if contract_ids:
                await db.execute(delete(OptionSnapshot).where(OptionSnapshot.option_contract_id.in_(contract_ids)))
                await db.execute(delete(OptionContract).where(OptionContract.id.in_(contract_ids)))
            await db.execute(delete(OptionChainSnapshot).where(OptionChainSnapshot.id.in_(chain_ids)))
        # Users first: their positions carry an FK to these instruments.
        for user_id in user_ids:
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
        await db.execute(delete(Instrument).where(Instrument.id.in_(ids)))
        await db.commit()


# --- the route ------------------------------------------------------------


async def test_the_endpoint_writes_a_chain_and_audits_it(require_infra, monkeypatch):
    """Behavioural proof at the call site. The ingestion function is
    covered by its own tests; what this asserts is that a route actually
    invokes it, which is the half three earlier rounds left uncovered."""
    prefix = f"AC{uuid.uuid4().hex[:5].upper()}"
    underlying, low, high = await _seed_instruments(prefix)
    stub = _StubChainProvider(_chain_payload(low.broker_instrument_key, high.broker_instrument_key))
    monkeypatch.setattr(admin_module, "market_data_provider_or_reason", lambda: (stub, ""))

    with TestClient(app) as client:
        headers, admin_id = await _make_admin(client, "chainadmin")
        try:
            r = client.post(
                "/admin/option-chain",
                json={"underlying": underlying.symbol, "expiry": low.expiry.isoformat()},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["snapshots_written"] == 2
            assert body["spot_price"] == 25123.45
            assert stub.calls == [(underlying.broker_instrument_key, low.expiry)]

            async with async_session_factory() as db:
                from app.options.snapshots import latest_option_snapshot

                assert await latest_option_snapshot(db, low.id) is not None
                audit = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.user_id == admin_id, AuditLog.action == "admin.option_chain_ingested"
                        )
                    )
                ).scalars().all()
                assert len(audit) == 1, "an admin writing market data must leave a trail"
        finally:
            await _cleanup([admin_id], [underlying, low, high])


async def test_a_non_admin_cannot_ingest_a_chain(require_infra):
    """Control. This route makes an outbound call on a rate-limited
    credential and writes rows every risk gate then reads."""
    with TestClient(app) as client:
        token, user_id = await _register(client, "notadmin")
        try:
            r = client.post(
                "/admin/option-chain",
                json={"underlying": "ANY", "expiry": date.today().isoformat()},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 403, r.text
        finally:
            await _cleanup([user_id], [])


async def test_ingesting_without_a_provider_says_what_to_configure(require_infra, monkeypatch):
    """Control. A deployment with no market-data token is misconfigured,
    not broken: 503 naming the remedy, never a 500."""
    import app.market.providers.factory as factory

    class _NoToken:
        upstox_data_access_token = None

    monkeypatch.setattr(factory, "get_settings", lambda: _NoToken())
    monkeypatch.setattr(admin_module, "market_data_provider_or_reason", factory.market_data_provider_or_reason)

    with TestClient(app) as client:
        headers, admin_id = await _make_admin(client, "chainnoprovider")
        try:
            r = client.post(
                "/admin/option-chain",
                json={"underlying": "ANY", "expiry": date.today().isoformat()},
                headers=headers,
            )
            assert r.status_code == 503, r.text
            assert "UPSTOX_DATA_ACCESS_TOKEN" in r.text
        finally:
            await _cleanup([admin_id], [])


async def test_an_unknown_underlying_is_a_404_not_a_500(require_infra, monkeypatch):
    """Control. An operator's typo must name the instrument, not produce a
    traceback several layers from the cause."""
    stub = _StubChainProvider(_chain_payload("NSE_FO|A", "NSE_FO|B"))
    monkeypatch.setattr(admin_module, "market_data_provider_or_reason", lambda: (stub, ""))

    with TestClient(app) as client:
        headers, admin_id = await _make_admin(client, "chain404")
        try:
            r = client.post(
                "/admin/option-chain",
                json={"underlying": "NO-SUCH-UNDERLYING", "expiry": date.today().isoformat()},
                headers=headers,
            )
            assert r.status_code == 404, r.text
            assert "NO-SUCH-UNDERLYING" in r.text
            assert stub.calls == [], "the provider must not be called for an instrument we do not have"
        finally:
            await _cleanup([admin_id], [])


async def test_an_underlying_with_no_provider_key_is_a_400(require_infra, monkeypatch):
    """Control. The instrument exists but carries no `broker_instrument_key`,
    so no provider can be asked about it -- a 400 naming the remedy."""
    prefix = f"NK{uuid.uuid4().hex[:5].upper()}"
    async with async_session_factory() as db:
        underlying = Instrument(
            symbol=f"{prefix}IDX", exchange="NSE", market=MarketType.EQUITY, instrument_type="INDEX", lot_size=1
        )
        db.add(underlying)
        await db.commit()
        await db.refresh(underlying)

    stub = _StubChainProvider(_chain_payload("NSE_FO|A", "NSE_FO|B"))
    monkeypatch.setattr(admin_module, "market_data_provider_or_reason", lambda: (stub, ""))

    with TestClient(app) as client:
        headers, admin_id = await _make_admin(client, "chainnokey")
        try:
            r = client.post(
                "/admin/option-chain",
                json={"underlying": underlying.symbol, "expiry": date.today().isoformat()},
                headers=headers,
            )
            assert r.status_code == 400, r.text
            assert "broker_instrument_key" in r.text
        finally:
            await _cleanup([admin_id], [underlying])


# --- and the gates it exists to feed --------------------------------------


async def test_ingesting_a_chain_makes_the_execution_gates_real(require_infra, monkeypatch):
    """Behavioural proof, end to end, and the round's headline.

    Before this writer existed, every `POST /options/execute` recorded none
    of `liquidity_acceptable`, `premium_matches_market` or
    `market_data_fresh` -- there was no quote to judge, and an earlier
    round removed the fabricated "passed" they used to claim. Here the
    chain is ingested through the real admin route and the strategy is
    executed through the real trading route; the assertion is on what the
    audit row actually holds afterwards.
    """
    prefix = f"GA{uuid.uuid4().hex[:5].upper()}"
    underlying, low, high = await _seed_instruments(prefix)
    stub = _StubChainProvider(_chain_payload(low.broker_instrument_key, high.broker_instrument_key))
    monkeypatch.setattr(admin_module, "market_data_provider_or_reason", lambda: (stub, ""))

    with TestClient(app) as client:
        admin_headers, admin_id = await _make_admin(client, "gatesadmin")
        trader_token, trader_id = await _register(client, "gatestrader")
        trader_headers = {"Authorization": f"Bearer {trader_token}"}
        r = client.post(
            "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=trader_headers
        )
        assert r.status_code == 200, r.text
        try:
            r = client.post(
                "/admin/option-chain",
                json={"underlying": underlying.symbol, "expiry": low.expiry.isoformat()},
                headers=admin_headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["snapshots_written"] == 2

            # Premium claimed at the real mid of the ingested quote.
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread",
                    "legs": [
                        # `quantity` is in lots; lot_size is 50 on these.
                        {"symbol": low.symbol, "direction": "LONG", "quantity": 1, "premium": 120.0},
                        {"symbol": high.symbol, "direction": "SHORT", "quantity": 1, "premium": 50.0},
                    ],
                },
                headers=trader_headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                events = (
                    await db.execute(select(RiskEvent).where(RiskEvent.user_id == trader_id))
                ).scalars().all()
                assert len(events) == 1
                checks = events[0].checks
                # All three, and each a real verdict rather than an absence.
                assert checks.get("liquidity_acceptable") is True, checks
                assert checks.get("premium_matches_market") is True, checks
                assert checks.get("market_data_fresh") is True, checks
        finally:
            await _cleanup([admin_id, trader_id], [underlying, low, high])


async def test_executing_without_ingesting_still_records_no_quote_checks(require_infra):
    """Control, and the half that keeps the test above honest.

    Same instruments, same strategy, no chain ingested. If these three
    checks appeared here too, the proof above would be showing that the
    endpoint always records them rather than that ingestion is what makes
    them real.
    """
    prefix = f"NG{uuid.uuid4().hex[:5].upper()}"
    underlying, low, high = await _seed_instruments(prefix)

    with TestClient(app) as client:
        trader_token, trader_id = await _register(client, "nogatestrader")
        trader_headers = {"Authorization": f"Bearer {trader_token}"}
        r = client.post(
            "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=trader_headers
        )
        assert r.status_code == 200, r.text
        try:
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread",
                    "legs": [
                        # `quantity` is in lots; lot_size is 50 on these.
                        {"symbol": low.symbol, "direction": "LONG", "quantity": 1, "premium": 120.0},
                        {"symbol": high.symbol, "direction": "SHORT", "quantity": 1, "premium": 50.0},
                    ],
                },
                headers=trader_headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                events = (
                    await db.execute(select(RiskEvent).where(RiskEvent.user_id == trader_id))
                ).scalars().all()
                assert len(events) == 1
                recorded = set(events[0].checks)
                assert not (recorded & {"liquidity_acceptable", "premium_matches_market", "market_data_fresh"}), (
                    f"claimed checks nothing performed: {sorted(recorded)}"
                )
        finally:
            await _cleanup([trader_id], [underlying, low, high])
