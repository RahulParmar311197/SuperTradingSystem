"""`cost_model` was a bare `dict` on both backtest routes.

Two separate failures, measured through the real endpoints on one strategy
over one set of candles.

**Four shapes returned HTTP 500 with a traceback.** `CostModel` is a slots
dataclass, so `CostModel(**payload.cost_model)` raises `TypeError` for an
unknown key, and the arithmetic raises `TypeError` for a string or a null.
`POST /backtest` did not guard the call at all, and `POST /backtest/validate`
guarded it with `except ValueError`, which never sees a `TypeError`.

**And values that constructed fine were unbounded.** `slippage_pct: -50`
means every fill comes in 50% better than the market:

    slippage_pct   0.05  ->  net_profit      8,106.55
    slippage_pct -50.0   ->  net_profit    224,464.33

Same strategy, same candles, a 27x edge that exists only in the cost model.
A backtest is the artifact someone decides to risk money on, and the cost
model is the one knob whose whole job is to stop it flattering the
strategy.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.backtest import CostModelRequest
from app.database.models.backtest import Backtest as BacktestRow
from app.database.models.backtest import BacktestMetrics as BacktestMetricsRow
from app.database.models.backtest import BacktestTrade as BacktestTradeRow
from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle

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

BOTH_ROUTES = ["/backtest", "/backtest/validate"]

# Each of these reached `CostModel(**...)` or its arithmetic and raised
# `TypeError`, which nothing on either route caught.
MALFORMED = [
    ("an unknown key", {"bogus": 1}),
    ("a string where a number goes", {"slippage_pct": "abc"}),
    ("an explicit null", {"slippage_pct": None}),
    ("a list", {"brokerage_pct": [1, 2]}),
]

# These constructed fine and produced a backtest anyway.
UNBOUNDED = [
    ("a negative slippage", {"slippage_pct": -50.0}),
    ("a negative brokerage", {"brokerage_pct": -1.0}),
    ("a cost larger than the position", {"taxes_pct": 500.0}),
]


class _Fixture:
    def __init__(self, client, headers, base, user_id, instrument_id, strategy_id):
        self.client, self.headers, self.base = client, headers, base
        self.user_id, self.instrument_id, self.strategy_id = user_id, instrument_id, strategy_id

    def post(self, route: str, **extra):
        return self.client.post(route, json={**self.base, **extra}, headers=self.headers)


async def _seed(client: TestClient, label: str) -> _Fixture:
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    ohlc = _UNIT * 4
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(ohlc)]

    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"CM{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", candles)
        symbol = instrument.symbol

    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Cost Model"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

    r = client.post(
        "/strategies",
        json={
            "name": "Bullish FVG retest",
            "market": symbol,
            "timeframe": "15m",
            "direction": "bullish",
            "conditions": [{"type": "fvg", "direction": "bullish"}],
            "entry": {"type": "fvg_retest"},
            "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    strategy_id = r.json()["id"]

    base = {
        "strategy_id": strategy_id,
        "instrument_id": str(instrument_id),
        "timeframe": "15m",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(minutes=len(ohlc))).isoformat(),
    }
    return _Fixture(client, headers, base, user_id, instrument_id, uuid.UUID(strategy_id))


async def _cleanup(f: _Fixture) -> None:
    async with async_session_factory() as db:
        # Children first: a successful run writes metrics and trades that
        # carry an FK to the backtest row.
        backtest_ids = (
            await db.execute(select(BacktestRow.id).where(BacktestRow.user_id == f.user_id))
        ).scalars().all()
        if backtest_ids:
            await db.execute(delete(BacktestMetricsRow).where(BacktestMetricsRow.backtest_id.in_(backtest_ids)))
            await db.execute(delete(BacktestTradeRow).where(BacktestTradeRow.backtest_id.in_(backtest_ids)))
        await db.execute(delete(BacktestRow).where(BacktestRow.user_id == f.user_id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == f.instrument_id))
        await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == f.strategy_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.id == f.strategy_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == f.user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == f.user_id))
        await db.execute(delete(Instrument).where(Instrument.id == f.instrument_id))
        await db.execute(delete(User).where(User.id == f.user_id))
        await db.commit()


# --- a malformed cost model must not be a 500 ----------------------------


@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize(("label", "cost_model"), MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_a_malformed_cost_model_is_422_not_500(require_infra, route, label, cost_model):
    """Behavioural proof, at both call sites.

    Parametrised over the routes deliberately: the two guarded this
    differently (one not at all, one with an `except ValueError` that a
    `TypeError` walks straight past), so a fix applied to one and not the
    other would look complete from either side alone.
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        f = await _seed(client, "cmbad")
        try:
            r = f.post(route, cost_model=cost_model)
            assert r.status_code == 422, f"{route} with {label}: {r.status_code} {r.text}"
            assert "Traceback" not in r.text
        finally:
            await _cleanup(f)


@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize(("label", "cost_model"), UNBOUNDED, ids=[m[0] for m in UNBOUNDED])
async def test_a_cost_that_flatters_the_strategy_is_rejected(require_infra, route, label, cost_model):
    """Behavioural proof, and the more serious half.

    These all constructed fine and produced a backtest. A negative cost is
    a subsidy paid on every fill, and the number it produces is reported
    with the same authority as a real one.
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        f = await _seed(client, "cmunb")
        try:
            r = f.post(route, cost_model=cost_model)
            assert r.status_code == 422, f"{route} with {label}: {r.status_code} {r.text}"
        finally:
            await _cleanup(f)


async def test_the_rejection_names_the_field_that_is_wrong(require_infra):
    """Behavioural proof. A 422 that does not say which key is unacceptable
    leaves the caller guessing at a model with six of them."""
    with TestClient(app, raise_server_exceptions=False) as client:
        f = await _seed(client, "cmmsg")
        try:
            r = f.post("/backtest", cost_model={"slippage_pct": -50.0})
            assert r.status_code == 422, r.text
            assert "slippage_pct" in r.text, r.text
        finally:
            await _cleanup(f)


# --- and legitimate cost models must be untouched ------------------------


async def test_a_well_formed_cost_model_still_runs_and_still_costs(require_infra):
    """Control, and the one that stops this becoming "reject everything".

    Two runs of the same strategy over the same candles: with costs and
    without. The costed run must succeed *and* report less profit --
    a cost model that is accepted but no longer applied would pass a test
    that only checked the status code.
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        f = await _seed(client, "cmgood")
        try:
            free = f.post("/backtest", cost_model={})
            costed = f.post("/backtest", cost_model={"slippage_pct": 0.05, "brokerage_pct": 0.03})
            assert free.status_code == 200, free.text
            assert costed.status_code == 200, costed.text
            assert costed.json()["net_profit"] < free.json()["net_profit"], (
                f"costs did not bite: free={free.json()['net_profit']} costed={costed.json()['net_profit']}"
            )
        finally:
            await _cleanup(f)


async def test_omitting_the_cost_model_entirely_is_still_allowed(require_infra):
    """Control. `cost_model` has always been optional, and a frictionless
    run is a legitimate first look at a strategy."""
    with TestClient(app, raise_server_exceptions=False) as client:
        f = await _seed(client, "cmnone")
        try:
            r = client.post("/backtest", json=f.base, headers=f.headers)
            assert r.status_code == 200, r.text
        finally:
            await _cleanup(f)


async def test_a_zero_cost_at_the_boundary_is_accepted(require_infra):
    """Control for the bound itself. `ge=0`, not `gt=0`: zero cost is the
    documented default and must stay valid, so the rejection is of
    *negative* costs specifically rather than of small ones."""
    with TestClient(app, raise_server_exceptions=False) as client:
        f = await _seed(client, "cmzero")
        try:
            r = f.post("/backtest", cost_model={"slippage_pct": 0.0, "brokerage_flat": 0.0})
            assert r.status_code == 200, r.text
        finally:
            await _cleanup(f)


# --- the conversion itself ------------------------------------------------


def test_the_request_model_carries_every_field_the_engine_reads():
    """Control, and a guard against the two drifting apart. A field added
    to `CostModel` and not here would silently fall back to its default,
    so a caller could set it and be ignored -- which is worse than a 422."""
    from dataclasses import fields

    from app.backtest.cost_model import CostModel

    assert {f.name for f in fields(CostModel)} == set(CostModelRequest.model_fields)


def test_the_conversion_preserves_the_values():
    """Control. The bounds are pointless if the numbers change on the way
    through."""
    request = CostModelRequest(
        brokerage_flat=20.0,
        brokerage_pct=0.03,
        slippage_pct=0.05,
        spread_pct=0.02,
        taxes_pct=0.1,
        contract_charges_flat=5.0,
    )
    cost_model = request.to_cost_model()

    assert cost_model.brokerage_flat == 20.0
    assert cost_model.slippage_pct == 0.05
    assert cost_model.taxes_pct == 0.1
    assert cost_model.contract_charges_flat == 5.0
