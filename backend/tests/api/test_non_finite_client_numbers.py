"""Two client-supplied floats had no bound, so NaN and Infinity got in.

Python's `json.loads` accepts the bare tokens `NaN`, `Infinity` and
`-Infinity` -- they are not valid JSON, but every JSON body this app
parses goes through it, so a client can send them. A Pydantic field with
no constraints takes them verbatim; a field with `gt=`/`lt=` rejects
them, because every comparison against NaN is False. That is why the
fields bounded by rounds 101/144/149/150 were already immune and these
two were the leftovers.

Measured, both through the real ASGI app:

1. `POST /ai/propose-trade`, `max_risk_percent`. It is the ceiling
   `validate_ai_trade_proposal` enforces, and against a proposal asking
   for 99% of the account:

       ceiling 1.0  -> valid=False, "risk_percent 99.0 exceeds ... 1.0%"
       ceiling NaN  -> valid=True, errors=[]
       ceiling inf  -> valid=True, errors=[]

   The endpoint never returned that false pass: it 500'd one step later
   instead, because the value is echoed into `AIDecision.input_context`,
   `json.dumps` emits a bare `NaN`, and Postgres refuses it with
   "invalid input syntax for type json" -- so the audit row this endpoint
   exists to write could not be written either.

2. `POST /options/strategy`, the payoff path's leg quotes. Nothing
   compared them, so they flowed into the payoff arithmetic and out into
   the response, where the serializer refused them:

       premium_call 120.0    -> 200, legs and payoff as expected
       premium_call NaN      -> ValueError: Out of range float values
                                are not JSON compliant
       premium_call Infinity -> the same

   `POST /options/execute`'s legs were already bounded (round 102), and
   that comment had noted non-finite values were rejected there only
   "incidentally"; on this path nothing rejected them at all.

NOT a third site, checked rather than assumed: `train_pct` and
`validation_pct` on `POST /backtest/validate` are unbounded in the
request model too, but `split_periods` validates them (`0 < pct < 1` and
`train + validation < 1`, all False for NaN) and the endpoint maps that
`ValueError` to a 422. A test below pins that, so the absence of a bound
there is a recorded decision rather than an oversight.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.ai.validation import validate_ai_trade_proposal
from app.auth.security import TokenType, decode_token
from app.database.models.ai import AIDecision, AIMessage
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.strategy.engine import StrategyEvaluationResult

JSON = {"Content-Type": "application/json"}


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"nf-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post(
        "/auth/register", json={"email": email, "password": "testpass123", "name": "NF"}
    ).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}", **JSON}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _cleanup(user_id: uuid.UUID) -> None:
    """Child rows first. `audit_logs` and `sessions` both reference
    `users`, and the AI endpoints here can write `ai_decisions` /
    `ai_messages` before failing, so all four go before the user."""
    async with async_session_factory() as db:
        for model in (AIDecision, AIMessage, AuditLog, UserSession):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# --- 1. the AI risk ceiling -----------------------------------------------


def _matched() -> StrategyEvaluationResult:
    return StrategyEvaluationResult(
        matched=True, satisfied=[], missing=[], direction="bullish",
        entry=100.0, stop=95.0, target=115.0, risk_reward=3.0,
    )


_GREEDY_PROPOSAL = {
    "decision": "TRADE", "direction": "bullish", "entry": 100.0, "stop": 95.0,
    "risk_reward": 3.0, "risk_percent": 99.0, "reasoning": "x",
}


def test_the_risk_ceiling_still_rejects_a_greedy_proposal():
    """Non-vacuity control: with a real ceiling the validator does its
    job, so the two assertions below are about the ceiling value and not
    about the check being dead."""
    result = validate_ai_trade_proposal(
        _GREEDY_PROPOSAL, _matched(), instrument_tradable=True, max_risk_percent=1.0
    )
    assert result.valid is False
    assert any("exceeds the maximum allowed" in e for e in result.errors)


@pytest.mark.parametrize("ceiling", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_the_validator_refuses_a_ceiling_it_cannot_enforce(ceiling):
    """The second layer, which the request-model bound cannot stand in
    for. This function is public and separately tested, so it decides for
    its own callers -- and it used to fail OPEN: against a NaN ceiling the
    same 99%-of-the-account proposal came back `valid=True, errors=[]`,
    because every comparison against NaN is False.

    Rebuilt around the fix rather than deleted. The earlier version of
    this test asserted the defect (`valid is True`) as the justification
    for bounding the field above it; with the guard in place the honest
    assertion is that the gate now fails CLOSED, and injection H (removing
    the guard) is what proves this still has teeth.
    """
    result = validate_ai_trade_proposal(
        _GREEDY_PROPOSAL, _matched(), instrument_tradable=True, max_risk_percent=ceiling
    )
    assert result.valid is False
    assert any("Unusable max_risk_percent" in e for e in result.errors), result.errors


def test_a_finite_ceiling_a_modest_proposal_respects_is_still_valid():
    """The other direction: the new guard must not refuse everything. A
    proposal inside its ceiling still validates, so the assertions above
    are about the ceiling being unusable and not about the function
    having been turned into a reject-all."""
    modest = {**_GREEDY_PROPOSAL, "risk_percent": 0.5}
    result = validate_ai_trade_proposal(
        modest, _matched(), instrument_tradable=True, max_risk_percent=1.0
    )
    assert result.valid is True, result.errors


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "0", "-1", "101"])
async def test_the_endpoint_refuses_a_ceiling_it_cannot_enforce(require_infra, raw):
    """422 from the request model, before any AI call, any arithmetic, or
    any audit row. `0` and `-1` ride along because the same bound closes
    them, and `101` because a percentage of an account cannot exceed 100."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            body = (
                '{"strategy_id": "%s", "instrument_id": "%s", "timeframe": "15m", '
                '"max_risk_percent": %s}' % (uuid.uuid4(), uuid.uuid4(), raw)
            )
            r = client.post("/ai/propose-trade", content=body, headers=headers)
            assert r.status_code == 422, r.text
            assert "max_risk_percent" in r.text
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize("raw", ["0.5", "1.0", "100"])
async def test_an_ordinary_ceiling_is_still_accepted(require_infra, raw):
    """The bound must not reject the values callers actually send. A 404
    for the made-up strategy id means the request model let it through,
    which is the whole point -- it got past validation to the handler."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            body = (
                '{"strategy_id": "%s", "instrument_id": "%s", "timeframe": "15m", '
                '"max_risk_percent": %s}' % (uuid.uuid4(), uuid.uuid4(), raw)
            )
            r = client.post("/ai/propose-trade", content=body, headers=headers)
            assert r.status_code == 404, r.text
        finally:
            await _cleanup(user_id)


# --- 2. the options payoff path -------------------------------------------


def _spread_body(premium: str, strike: str = "25000") -> str:
    return (
        '{"strategy_name": "bull_call_spread",'
        ' "strategy_kwargs": {"long_strike": 25000, "short_strike": 25200},'
        ' "legs_by_strike": {'
        '"25000": {"strike": %s, "premium_call": %s},'
        '"25200": {"strike": 25200, "premium_call": 40.0}},'
        ' "quantity": 1, "lot_size": 50}' % (strike, premium)
    )


async def test_the_payoff_endpoint_still_answers_an_ordinary_spread(require_infra):
    """Non-vacuity control: the fixture below is a request this endpoint
    genuinely serves, so the 422s are about the value and not the shape."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/strategy", content=_spread_body("120.0"), headers=headers)
            assert r.status_code == 200, r.text
            assert len(r.json()["legs"]) == 2
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize("premium", ["NaN", "Infinity", "-Infinity", "0", "-5"])
async def test_a_non_finite_premium_is_refused_not_computed_with(require_infra, premium):
    """Before the bound these reached the payoff arithmetic and the
    response serializer raised `ValueError: Out of range float values are
    not JSON compliant` -- a 500 on a read-only endpoint whose answer is
    what someone reads before choosing a strategy."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/strategy", content=_spread_body(premium), headers=headers)
            assert r.status_code == 422, r.text
            assert "premium_call" in r.text
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize("strike", ["NaN", "Infinity", "0", "-25000"])
async def test_a_non_finite_strike_is_refused_too(require_infra, strike):
    """The same field group: a strike is a price and carries the same
    bound. Separately parametrised because `strike` and `premium_call`
    are different fields and a fix could easily reach only one."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/strategy", content=_spread_body("120.0", strike=strike), headers=headers)
            assert r.status_code == 422, r.text
            assert "strike" in r.text
        finally:
            await _cleanup(user_id)


# --- 3. the site that is NOT a third one ----------------------------------


async def test_a_non_finite_backtest_split_is_a_422_from_the_splitter(require_infra):
    """`train_pct`/`validation_pct` are unbounded in the request model on
    purpose: `split_periods` already rejects anything outside (0, 1) --
    NaN included, since `0 < nan` is False -- and the endpoint maps that
    `ValueError` to a 422. Recorded so the absence of a bound there stays
    a decision. A 404 for the made-up strategy id is the earlier guard;
    what matters is that neither is a 500."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            body = (
                '{"strategy_id": "%s", "instrument_id": "%s", "timeframe": "15m",'
                ' "start_date": "2026-01-01T00:00:00Z", "end_date": "2026-02-01T00:00:00Z",'
                ' "train_pct": NaN, "validation_pct": 0.2}' % (uuid.uuid4(), uuid.uuid4())
            )
            r = client.post("/backtest/validate", content=body, headers=headers)
            assert r.status_code in (404, 422), r.text
        finally:
            await _cleanup(user_id)


def test_split_periods_itself_rejects_a_non_finite_split():
    """The layer that actually does that work, asserted directly -- the
    endpoint test above can only reach it once a real strategy and
    candles exist."""
    from app.backtest.validation import split_periods
    from app.smc.types import Candle
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), 100, 101, 99, 100, 10) for i in range(50)]
    for bad in (float("nan"), float("inf"), 0.0, 1.0, -0.5):
        with pytest.raises(ValueError):
            split_periods(candles, train_pct=bad, validation_pct=0.2)
    # ...and a real split still works.
    assert split_periods(candles, train_pct=0.6, validation_pct=0.2) is not None
