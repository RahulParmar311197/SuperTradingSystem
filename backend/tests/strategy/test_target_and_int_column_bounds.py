"""Three client numbers reached columns that could not hold them.

All measured against the live endpoints before the bounds below:

    POST /auto-trading/enable {"max_positions": 2147483648}
        -> 500  asyncpg DataError: invalid input for query argument
           (2147483647 answers 200 -- the boundary is exactly int4, which
           is what `users.auto_trading_max_positions` is declared as)
    POST /auto-trading/enable {"max_trades_per_day": 3000000000}
        -> 500  the same
    POST /strategies with risk.minimum_rr = Infinity
        -> 500  InvalidTextRepresentationError: `strategies.definition` is
           a JSON column and Postgres' JSON has no `Infinity` token
    POST /strategies with risk.minimum_rr = 1e308
        -> 201, stored happily -- and then a paper session on that strategy
           ran normally until the bar it first entered on, where the
           request 500'd with NumericValueOutOfRangeError from
           `persist_position`. Measured on that run, scoped to the
           strategy: zero `positions` rows and zero `trades` rows, against
           a control at minimum_rr=2.0 on the identical candles that
           journalled a position and a trade of +1000.

The last one is the interesting one, and it is round 149's lesson landing
in a second file: what overflows `positions.target` is not `minimum_rr`
itself but the PRODUCT `risk_per_unit * minimum_rr` that
`app/strategy/engine.py` computes from it. So the DSL bound is only half
the fix -- a `minimum_rr` well inside that ceiling still overflows against
a large enough `risk_per_unit`, and the engine guards the product where it
is formed. The two auto-trading integers are the contrast: they are only
ever compared against a count, never multiplied, so there a field bound
really is the whole fix.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.database.models.risk import AuditLog
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.ict.engine import ICTConfig, ICTEngine
from app.main import app
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from app.strategy.engine import _MAX_JOURNALLED_TARGET, StrategyEngine, _is_journallable
from tests.smc.conftest import make_candles

# Exactly `tests/strategy/test_engine.py`'s setup: a bullish sweep into an
# FVG that price retests. Reused rather than re-derived so this file's
# "an ordinary strategy still matches" control means the same thing there.
BULLISH_SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
]


def _context(candles):
    return EvaluationContext(
        symbol="TESTSYM",
        timeframe="15m",
        timestamp=candles[-1].timestamp,
        current_price=candles[-1].close,
        smc=SMCEngine(SMCConfig(swing_length=2)).analyze(candles),
        ict=ICTEngine(ICTConfig()).analyze(candles),
    )


def _strategy(minimum_rr: float) -> StrategyDefinition:
    return StrategyDefinition(
        name="Bullish FVG retest",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[Condition(type=ConditionType.FVG, direction="bullish")],
        entry=EntryConfig(type="fvg_retest"),
        risk=RiskConfig(risk_percent=1.0, minimum_rr=minimum_rr),
    )


# --- the engine's product guard, where no field bound can stand in ------


def test_a_target_the_journal_cannot_hold_does_not_produce_a_signal():
    """Behavioural proof at the engine layer, and the whole argument for
    this guard existing.

    `minimum_rr=1e12` is the largest value the DSL accepts -- so this
    strategy passes every field bound there is. This fixture's stop sits
    3.3 points from its entry, an entirely ordinary distance, and 3.3e12
    does not fit `positions.target`. A per-field bound cannot reach this
    case at all: only the product shows it.

    (Written at 1e11 first, which this fixture does NOT overflow --
    3.3e11 fits -- so the test failed and said so. The number below is
    derived from the fixture rather than guessed.)
    """
    context = _context(make_candles(BULLISH_SETUP))
    ordinary = StrategyEngine().evaluate(_strategy(minimum_rr=2.0), context)
    risk_per_unit = abs(ordinary.entry - ordinary.stop)
    assert risk_per_unit == pytest.approx(3.3), risk_per_unit
    assert risk_per_unit * 1e12 > _MAX_JOURNALLED_TARGET, "the fixture must actually overflow"

    result = StrategyEngine().evaluate(_strategy(minimum_rr=1e12), context)
    assert result.matched is False, f"target {result.target} should have been refused"
    assert "target_out_of_range" in result.missing, result.missing


def test_an_ordinary_target_is_unaffected_and_still_carries_its_numbers():
    """Control, with the arithmetic pinned rather than just the verdict. A
    guard that refused everything, or one that clamped the target instead
    of refusing it, would both pass a bare `matched is True`."""
    context = _context(make_candles(BULLISH_SETUP))
    result = StrategyEngine().evaluate(_strategy(minimum_rr=2.0), context)

    assert result.matched is True, result.missing
    risk_per_unit = abs(result.entry - result.stop)
    assert result.target == pytest.approx(result.entry + risk_per_unit * 2.0), (
        result.entry, result.stop, result.target
    )


def test_the_guard_is_reachable_from_inside_the_dsl_ceiling():
    """Proof that the engine guard is not dead code.

    The DSL rejects `minimum_rr` above 1e12, so if every value it *does*
    accept produced a journallable target, the engine's check could be
    deleted with nothing noticing -- which is how round 145's unreachable
    `None` branch was found. These pairs are all accepted by the DSL.
    """
    for minimum_rr, risk_per_unit in ((1e11, 100.0), (1e6, 1e7), (1e12, 1.0001)):
        RiskConfig(risk_percent=1.0, minimum_rr=minimum_rr)  # the DSL accepts it
        assert not _is_journallable(100.0 + risk_per_unit * minimum_rr), (minimum_rr, risk_per_unit)


@pytest.mark.parametrize(
    "value,journallable",
    [
        (float("inf"), False),
        (float("-inf"), False),
        (float("nan"), False),
        (_MAX_JOURNALLED_TARGET, False),          # the ceiling itself: Numeric(18, 6) cannot hold 1e12
        (_MAX_JOURNALLED_TARGET - 1.0, True),     # one below it
        (-_MAX_JOURNALLED_TARGET + 1.0, True),    # a short's target, on the other side of zero
        (0.05, True),
    ],
)
def test_is_journallable_is_exact_at_its_edges(value, journallable):
    """Control. `abs(nan) < x` is False, so NaN is refused by the magnitude
    test even without the `isfinite` call -- both are asserted here so the
    two halves cannot silently become one."""
    assert _is_journallable(value) is journallable, value


def test_nan_is_refused_by_the_magnitude_test_alone():
    """Control, and a recorded negative result.

    `_is_journallable` was first written as
    `math.isfinite(price) and abs(price) < _MAX_JOURNALLED_TARGET`, and
    injecting the `isfinite` call away left all of these tests green --
    every comparison against NaN is False, so the magnitude test refuses
    it unaided, and `inf` fails that test outright. The redundant half was
    removed; this pins the property the remaining half relies on, so that
    if the magnitude test is ever replaced by something NaN could satisfy,
    this fails rather than the guard quietly opening.
    """
    assert (float("nan") < _MAX_JOURNALLED_TARGET) is False
    assert (float("nan") > _MAX_JOURNALLED_TARGET) is False
    assert _is_journallable(float("nan")) is False
    assert _is_journallable(float("inf")) is False


# --- the DSL bound, which is what stops the value being STORED ----------


DSL_REFUSED = [float("inf"), float("-inf"), 1e308, 1e12 + 1, 0.0, -1.0]


@pytest.mark.parametrize("minimum_rr", DSL_REFUSED, ids=[str(v) for v in DSL_REFUSED])
def test_a_minimum_rr_that_cannot_be_stored_is_refused(minimum_rr):
    """Behavioural proof. `Infinity` used to 500 at `POST /strategies`
    (Postgres JSON has no such token) and 1e308 used to store and then 500
    on the first bar the strategy entered on."""
    with pytest.raises(Exception) as excinfo:
        RiskConfig(risk_percent=1.0, minimum_rr=minimum_rr)
    assert "minimum_rr" in str(excinfo.value), str(excinfo.value)[:300]


@pytest.mark.parametrize("minimum_rr", [2.0, 0.5, 1e12, 1e6])
def test_a_usable_minimum_rr_is_still_accepted(minimum_rr):
    """Control against over-tightening, including the ceiling itself and a
    sub-1 ratio -- scalping a target nearer than the stop is a real, if
    unfashionable, thing to ask for."""
    assert RiskConfig(risk_percent=1.0, minimum_rr=minimum_rr).minimum_rr == minimum_rr


# --- the two integers, where a field bound IS the whole fix -------------


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"int4-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "I4"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    async with async_session_factory() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(trading_permissions=[TradingPermission.AUTO_TRADE.value])
        )
        await db.commit()
    return {"Authorization": f"Bearer {token}"}, user_id


async def _cleanup(user_id: uuid.UUID) -> None:
    """Child rows first -- audit_logs and sessions both reference the user."""
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


OVER_INT4 = [("max_positions", 2_147_483_648), ("max_trades_per_day", 3_000_000_000), ("max_positions", 10**18)]


@pytest.mark.parametrize("field,value", OVER_INT4, ids=[f"{f}={v}" for f, v in OVER_INT4])
async def test_a_value_an_int4_column_cannot_hold_is_refused(field, value, require_infra):
    """Behavioural proof. Each of these reached asyncpg and 500'd."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/auto-trading/enable", json={"confirm": True, field: value}, headers=headers)
            assert r.status_code == 422, f"{field}={value}: {r.status_code} {r.text[:200]}"
            assert any(field in str(d.get("loc", "")) for d in r.json()["detail"]), r.text[:300]
        finally:
            await _cleanup(user_id)


async def test_the_largest_value_int4_can_hold_is_still_accepted_and_stored(require_infra):
    """Control, and the one that pins the bound to the column rather than
    to a round number. 2147483647 is int4's maximum and was measured
    storing correctly before the bound existed, so refusing it would be
    this endpoint declining a value its own column can hold. Read back
    from the DB, not just from the response, so a bound that accepted the
    request and then wrote something else would still fail."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post(
                "/auto-trading/enable",
                json={"confirm": True, "max_positions": 2_147_483_647, "max_trades_per_day": 2_147_483_647},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["max_positions"] == 2_147_483_647, r.json()

            async with async_session_factory() as db:
                stored = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
                assert stored.auto_trading_max_positions == 2_147_483_647
                assert stored.auto_trading_max_trades_per_day == 2_147_483_647
        finally:
            await _cleanup(user_id)


async def test_ordinary_auto_trading_settings_are_unchanged(require_infra):
    """Control. A bound that rejected everything, or one that quietly
    altered the value, would satisfy the proofs above."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post(
                "/auto-trading/enable",
                json={"confirm": True, "risk_per_trade_pct": 0.25, "max_positions": 3, "max_trades_per_day": 7},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["enabled"] is True
            assert body["max_positions"] == 3
            assert body["max_trades_per_day"] == 7
            assert body["risk_per_trade_pct"] == pytest.approx(0.25)
        finally:
            await _cleanup(user_id)
