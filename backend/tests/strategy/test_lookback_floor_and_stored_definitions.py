"""Two findings about strategies that look alive and do nothing.

**One: `lookback` below 1 can never match.** `evaluate_condition` asks
`context.current_index - event.index < condition.lookback` for `bos`,
`mss`, `choch` and `liquidity_sweep`. On the bar the event printed that
difference is 0, so 1 is the smallest value that can ever be true.
Measured through `StrategyEngine.evaluate` against a real BOS at index 7,
evaluated one bar later:

    lookback=2   -> satisfied=['bos']
    lookback=1   -> missing=['bos']    (correct: the event is a bar old)
    lookback=0   -> missing=['bos']
    lookback=-5  -> missing=['bos']

and directly at the evaluator, on the event bar itself:

    lookback=1 -> True      lookback=0 -> False      lookback=-1000 -> False

`POST /strategies` answered **201** for the `lookback=0` version. Because
conditions AND implicitly, one of them zeroes the whole strategy: it
stores, lists, backtests and auto-trades like any other and simply never
produces a signal. That is the same ruling `app/strategy/dsl.py` already
makes for unfed condition types (round 88), for `premium_discount` with
no zone (round 113) and for unknown entry types.

**Two: a stored definition that no longer validates 500s.**
`strategies.definition` is written by the DSL of the day and re-validated
by the DSL of today, and every validator ever added widens that gap.
Measured by planting a definition that trips round 88's rule and posting a
backtest for it:

    POST /backtest -> pydantic_core.ValidationError, uncaught,
                      app/api/backtest.py:132

`ScannerWorker` and `AutoTradeSupervisor` already handle this — both wrap
the same call per strategy in `try/except`, log, and carry on. Six API
routes did not.

The two findings belong together: without the second fix, the first would
turn every already-stored `lookback=0` strategy from silently-dead into a
500.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete

from app.ict.engine import ICTConfig, ICTEngine
from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow, StrategyVersion
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from app.strategy.engine import StrategyEngine
from app.strategy.evaluator import evaluate_condition
from app.main import app
from tests.smc.conftest import make_candles
from tests.smc.test_swings import OHLC

# `asyncio_mode = auto` (pytest.ini) runs the async tests below without a
# marker, and a module-level `pytest.mark.asyncio` would warn on every sync
# test in this file -- this one deliberately mixes the two, because the
# authoring rule is synchronous and the stored-row handling is not.

# The condition types whose evaluator arm actually reads `lookback`. Listed
# here rather than derived from the module, so both sides of the assertions
# below do not come from the same value.
EVENT_TYPES = [
    ConditionType.BOS,
    ConditionType.MSS,
    ConditionType.CHOCH,
    ConditionType.LIQUIDITY_SWEEP,
]


def _bos_context(current_index: int) -> tuple[EvaluationContext, int]:
    candles = make_candles(OHLC)
    smc = SMCEngine(SMCConfig(swing_length=2)).analyze(candles)
    bos = next(e for e in smc.structure_events if e.event_type.value == "BOS")
    assert bos.index == 7  # pinned by tests/smc/test_structure.py
    ict = ICTEngine(ICTConfig()).analyze(candles)
    return (
        EvaluationContext(
            symbol="TESTSYM",
            timeframe="15m",
            timestamp=candles[-1].timestamp,
            current_price=candles[-1].close,
            smc=smc,
            ict=ict,
            current_index=current_index,
        ),
        bos.index,
    )


# --- layer 1: the authoring rule -----------------------------------------


@pytest.mark.parametrize("condition_type", EVENT_TYPES, ids=[t.value for t in EVENT_TYPES])
@pytest.mark.parametrize("lookback", [0, -1, -1000])
def test_a_lookback_that_can_never_match_is_refused(condition_type, lookback):
    """Behavioural proof. Every type whose evaluator reads `lookback`, at
    every value that cannot produce a match."""
    with pytest.raises(ValidationError) as exc:
        Condition(type=condition_type, lookback=lookback)
    assert "can never match" in str(exc.value)


@pytest.mark.parametrize("lookback", [1, 2, 30, 10_000])
def test_a_usable_lookback_is_accepted(lookback):
    """Control against over-tightening. 1 is legitimate and means "only on
    the bar the event printed"; a very large value means "this event never
    expires", which is what every structure condition did before the expiry
    window existed, so there is deliberately no upper bound."""
    assert Condition(type=ConditionType.BOS, lookback=lookback).lookback == lookback


def test_an_omitted_lookback_still_gets_its_per_type_default():
    """Control. The floor must not disturb the fill-in that runs before it
    — a `None` reaching the new check would otherwise be refused."""
    assert Condition(type=ConditionType.BOS).lookback == 30
    assert Condition(type=ConditionType.FVG).lookback == 5


def test_one_is_the_smallest_lookback_that_can_ever_be_true():
    """Behavioural proof of *where* the floor belongs, measured rather than
    asserted from the constant. Off-by-one here would make the rule wrong
    in either direction: a floor of 2 refuses a usable value, a floor of 0
    admits an unusable one."""
    context, bos_index = _bos_context(current_index=7)  # the event bar itself
    assert evaluate_condition(Condition(type=ConditionType.BOS, direction="bullish", lookback=1), context) is True
    assert bos_index == context.current_index

    later, _ = _bos_context(current_index=8)  # one bar after
    assert evaluate_condition(Condition(type=ConditionType.BOS, direction="bullish", lookback=1), later) is False
    assert evaluate_condition(Condition(type=ConditionType.BOS, direction="bullish", lookback=2), later) is True


def test_the_whole_strategy_is_refused_not_just_the_condition():
    """Behavioural proof at the level that matters. Conditions AND, so a
    definition carrying one unsatisfiable condition is a definition that
    can never fire — it must not be constructible."""
    with pytest.raises(ValidationError):
        StrategyDefinition(
            name="zero lookback",
            market="TESTSYM",
            timeframe="15m",
            direction="bullish",
            conditions=[Condition(type=ConditionType.BOS, direction="bullish", lookback=0)],
            entry=EntryConfig(type="market"),
            risk=RiskConfig(risk_percent=1.0, minimum_rr=1.0),
        )


def test_a_strategy_with_a_usable_lookback_still_evaluates():
    """Control at the same level, through the engine the scanner and the
    paper engine both call. Without this, refusing everything would pass
    every test above."""
    context, _ = _bos_context(current_index=8)
    strategy = StrategyDefinition(
        name="bos",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[Condition(type=ConditionType.BOS, direction="bullish", lookback=2)],
        entry=EntryConfig(type="market"),
        risk=RiskConfig(risk_percent=1.0, minimum_rr=1.0),
    )
    assert StrategyEngine().evaluate(strategy, context).satisfied == ["bos"]


# --- the same rule, at the endpoint --------------------------------------


_VALID_DEFINITION = {
    "name": "bos strategy",
    "market": "TESTSYM",
    "timeframe": "15m",
    "direction": "bullish",
    "conditions": [{"type": "bos", "direction": "bullish", "lookback": 30}],
    "entry": {"type": "market"},
    "risk": {"risk_percent": 1.0, "minimum_rr": 1.0},
}


def _zeroed() -> dict:
    definition = {**_VALID_DEFINITION, "name": "zero lookback"}
    definition["conditions"] = [{**definition["conditions"][0], "lookback": 0}]
    return definition


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"lookback-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "L"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _cleanup(user_id: uuid.UUID) -> None:
    """Child rows first: strategy_versions reference strategies, sessions
    and strategies reference the user."""
    async with async_session_factory() as db:
        strategy_ids = (
            await db.execute(StrategyRow.__table__.select().with_only_columns(StrategyRow.id).where(StrategyRow.user_id == user_id))
        ).scalars().all()
        for strategy_id in strategy_ids:
            await db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_post_strategies_refuses_a_zero_lookback(require_infra):
    """Behavioural proof at the call site. This answered 201 before."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/strategies", json=_zeroed(), headers=headers)
            assert r.status_code == 422, r.text
            assert "can never match" in r.text
        finally:
            await _cleanup(user_id)


async def test_post_strategies_still_accepts_a_usable_one(require_infra):
    """Control. The endpoint must keep working for ordinary strategies."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/strategies", json=_VALID_DEFINITION, headers=headers)
            assert r.status_code == 201, r.text
            assert r.json()["definition"]["conditions"][0]["lookback"] == 30
        finally:
            await _cleanup(user_id)


# --- layer 2: what to do about the rows already stored --------------------
#
# The authoring rule cannot reach a row that is already in the table, and
# these plant one directly — the shape of a definition written before any
# given validator existed.


async def _plant(user_id: uuid.UUID, definition: dict) -> uuid.UUID:
    async with async_session_factory() as db:
        row = StrategyRow(user_id=user_id, name=definition["name"], definition=definition, is_active=True, version=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


# Two separate DSL rules, because the point is that this covers *any*
# definition the current DSL rejects, not just the one added here.
STALE_DEFINITIONS = {
    "lookback=0 (this round's rule)": _zeroed(),
    "an unfed condition type (round 88's rule)": {
        **_VALID_DEFINITION,
        "name": "unfed",
        "conditions": [{"type": "indicator", "operator": "greater_than", "value": 0}],
    },
}


@pytest.mark.parametrize("label", list(STALE_DEFINITIONS), ids=list(STALE_DEFINITIONS))
async def test_a_stored_definition_that_no_longer_validates_is_a_422(label, require_infra):
    """Behavioural proof. `POST /backtest` raised an uncaught
    `ValidationError` from `app/api/backtest.py:132` — a 500 with a
    traceback, for stored data that is merely out of date, on the very
    endpoint whose job is to tell the author whether a strategy works."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        strategy_id = await _plant(user_id, STALE_DEFINITIONS[label])
        try:
            r = client.post(
                "/backtest",
                json={
                    "strategy_id": str(strategy_id),
                    "instrument_id": str(uuid.uuid4()),
                    "timeframe": "15m",
                    "start_date": "2026-01-01T00:00:00Z",
                    "end_date": "2026-02-01T00:00:00Z",
                },
                headers=headers,
            )
            assert r.status_code == 422, r.text
            detail = r.json()["detail"]
            assert str(strategy_id) in detail, detail
            assert "no longer valid" in detail, detail
        finally:
            await _cleanup(user_id)


async def test_the_scanner_route_answers_the_same_way(require_infra):
    """Behavioural proof at a second route, because the fix is shared and
    a single-route test would pass if only that one had been changed."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        strategy_id = await _plant(user_id, STALE_DEFINITIONS["lookback=0 (this round's rule)"])
        try:
            r = client.post(
                "/scanner",
                json={"strategy_id": str(strategy_id), "instrument_ids": [str(uuid.uuid4())], "timeframe": "15m"},
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert "no longer valid" in r.json()["detail"]
        finally:
            await _cleanup(user_id)


async def test_a_valid_stored_definition_is_not_disturbed(require_infra):
    """Control. The helper must only intercept definitions that genuinely
    fail to parse — a version that rejected everything would satisfy both
    proofs above."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        strategy_id = await _plant(user_id, _VALID_DEFINITION)
        try:
            r = client.post(
                "/backtest",
                json={
                    "strategy_id": str(strategy_id),
                    "instrument_id": str(uuid.uuid4()),
                    "timeframe": "15m",
                    "start_date": "2026-01-01T00:00:00Z",
                    "end_date": "2026-02-01T00:00:00Z",
                },
                headers=headers,
            )
            # It gets past loading and fails on the *data*, which is the
            # honest answer for an instrument with no candles — and is a
            # different message from the one above.
            assert r.status_code == 422, r.text
            assert "no longer valid" not in r.json()["detail"], r.text
            assert "candles" in r.json()["detail"], r.text
        finally:
            await _cleanup(user_id)
