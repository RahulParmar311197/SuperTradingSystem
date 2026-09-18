"""`POST /options/strategy` 500'd on its own default request.

`strategy_kwargs: dict = {}` is spread into `build_strategy(...)`, and
**every one of the ten builders requires at least one strike argument** --
`long_call` needs a `strike`, `iron_condor` needs four. So the field's
default could not succeed for any strategy: it raised `TypeError` (missing
positional arguments), the route caught only `(ValueError, KeyError)`, and
the caller got HTTP 500 with a traceback. Nothing anywhere told them what
to send instead, so the only way to discover it was to keep guessing.

Three more shapes did the same: an unknown kwarg, and `quantity` or
`chain` passed again through `strategy_kwargs` ("multiple values for
argument").

Separately, `quantity` and `lot_size` were unbounded. This endpoint
answers a question rather than placing a trade, so what that produced was
a wrong number rather than a bad fill -- which is worse to leave than it
sounds, because a payoff summary is exactly what someone reads *before*
choosing a strategy. Measured on a 25000/25200 bull call spread:

    quantity=-5   -> 200, net_premium=-17500 (a debit spread as a credit)
    lot_size=0    -> 200, every number 0.0
    lot_size=-50  -> 200, every number inverted
"""

import inspect
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.options.strategies import _STRATEGY_BUILDERS, BIAS_STRATEGIES, build_strategy, required_arguments

ALL_STRATEGIES = sorted(_STRATEGY_BUILDERS)

_CHAIN = {
    str(strike): {"strike": strike, "premium_call": 120.0 - i * 20, "premium_put": 80.0 + i * 20}
    for i, strike in enumerate((24800, 25000, 25200, 25400))
}


def _base(strategy_name: str = "bull_call_spread", **overrides) -> dict:
    body = {
        "strategy_name": strategy_name,
        "legs_by_strike": _CHAIN,
        "quantity": 1,
        "lot_size": 50,
        "strategy_kwargs": {"long_strike": 25000, "short_strike": 25200},
    }
    body.update(overrides)
    return body


async def _login(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"optstrat-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Opt Strat"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _cleanup(user_id: uuid.UUID) -> None:
    """Children before parents, every time.

    Registering and logging in writes `audit_logs` and `sessions` rows that
    carry an FK to the user, so deleting the user first is a
    `ForeignKeyViolationError` -- a teardown that fails on a test whose
    assertions all passed, which reads as a bug in the code under test
    rather than in the fixture. That has now happened to me in four
    consecutive rounds, in four different tables.
    """
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# --- nothing may reach the caller as a 500 --------------------------------


@pytest.mark.parametrize("strategy_name", ALL_STRATEGIES)
async def test_the_default_request_is_422_for_every_strategy(require_infra, strategy_name):
    """Behavioural proof, over the whole registry.

    Parametrised deliberately: this is not one strategy's quirk. The
    request model's own default `strategy_kwargs={}` is unsatisfiable for
    all ten builders, so the documented default request was a guaranteed
    500 whichever strategy you asked for.
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _login(client)
        try:
            body = _base(strategy_name)
            del body["strategy_kwargs"]
            r = client.post("/options/strategy", json=body, headers=headers)

            assert r.status_code == 422, r.text
            assert "Traceback" not in r.text
            # And it says what to send, rather than only that this failed.
            for argument in required_arguments(strategy_name):
                assert argument in r.text, f"the rejection must name {argument}: {r.text}"
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize(
    ("label", "strategy_kwargs"),
    [
        ("an unknown kwarg", {"long_strike": 25000, "short_strike": 25200, "bogus": 1}),
        ("quantity passed twice", {"long_strike": 25000, "short_strike": 25200, "quantity": 5}),
        ("lot_size passed twice", {"long_strike": 25000, "short_strike": 25200, "lot_size": 5}),
        ("chain passed as a kwarg", {"long_strike": 25000, "short_strike": 25200, "chain": {}}),
    ],
    ids=["unknown", "quantity", "lot_size", "chain"],
)
async def test_a_bad_strategy_kwargs_is_422_not_500(require_infra, label, strategy_kwargs):
    """Behavioural proof. Each of these raised `TypeError` out of the
    `**kwargs` spread, which the route's `except (ValueError, KeyError)`
    never saw."""
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post("/options/strategy", json=_base(strategy_kwargs=strategy_kwargs), headers=headers)
            assert r.status_code == 422, f"{label}: {r.status_code} {r.text}"
            assert "Traceback" not in r.text
        finally:
            await _cleanup(user_id)


async def test_a_reserved_argument_says_why_it_is_refused(require_infra):
    """Behavioural proof. "multiple values for argument 'quantity'" is a
    Python error message, not an explanation; the caller needs to know that
    the field is theirs to set once, at the top level."""
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post(
                "/options/strategy",
                json=_base(strategy_kwargs={"long_strike": 25000, "short_strike": 25200, "quantity": 5}),
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert "quantity" in r.text and "strategy_kwargs" in r.text, r.text
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("negative quantity", {"quantity": -5}),
        ("zero quantity", {"quantity": 0}),
        ("zero lot_size", {"lot_size": 0}),
        ("negative lot_size", {"lot_size": -50}),
    ],
    ids=["neg-qty", "zero-qty", "zero-lot", "neg-lot"],
)
async def test_a_size_that_inverts_the_payoff_is_rejected(require_infra, label, overrides):
    """Behavioural proof. Each of these used to return 200 with numbers
    that describe a position nobody can hold -- a debit spread reported as
    a credit, or a strategy of zero contracts summarised as if it were
    real."""
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post("/options/strategy", json=_base(**overrides), headers=headers)
            assert r.status_code == 422, f"{label}: {r.status_code} {r.text}"
        finally:
            await _cleanup(user_id)


# --- and a caller can find out what to send -------------------------------


async def test_the_strategy_list_reports_what_each_one_requires(require_infra):
    """Behavioural proof. Without this the 422 above is still the only way
    to learn the argument names, which is discovery by trial and error
    against a rate-limited authenticated endpoint."""
    with TestClient(app) as client:
        headers, user_id = await _login(client)
        try:
            r = client.get("/options/strategies", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["by_bias"] == BIAS_STRATEGIES
            requires = body["requires"]
            assert requires["long_call"] == ["strike"]
            assert requires["bull_call_spread"] == ["long_strike", "short_strike"]
            assert len(requires["iron_condor"]) == 4
            # Every strategy the list offers must be answerable.
            for names in BIAS_STRATEGIES.values():
                for name in names:
                    assert requires[name], f"{name} reports no required arguments"
        finally:
            await _cleanup(user_id)


def test_required_arguments_reads_the_builders_rather_than_a_hard_coded_list():
    """Control. The point of introspecting is that a builder which gains or
    loses a strike cannot drift away from what the API advertises -- so
    this compares against `inspect` directly, not against a literal."""
    for name, builder in _STRATEGY_BUILDERS.items():
        expected = [
            p.name
            for p in inspect.signature(builder).parameters.values()
            if p.default is inspect.Parameter.empty and p.name != "chain"
        ]
        assert required_arguments(name) == expected, name
        assert expected, f"{name} would need no arguments, which would make this test vacuous"


# --- what must still work -------------------------------------------------


async def test_a_correct_two_leg_request_still_builds(require_infra):
    """Control, and the one that stops this becoming "reject everything".
    The numbers are pinned, not just the status: a 120/50 spread on 1 lot
    of 50 is a 3500 debit."""
    with TestClient(app) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post("/options/strategy", json=_base(), headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["legs"]) == 2
            # Derived from the fixture, so it stays a real check on the
            # arithmetic if the chain above ever changes: a bull call
            # spread pays the long call's premium and receives the short
            # one's, over `quantity * lot_size` contracts.
            long_premium = _CHAIN["25000"]["premium_call"]
            short_premium = _CHAIN["25200"]["premium_call"]
            expected_debit = (long_premium - short_premium) * 1 * 50
            assert expected_debit > 0, "the fixture must be a debit spread for this to mean anything"
            assert body["net_premium"] == pytest.approx(expected_debit)
            assert body["max_loss"] == pytest.approx(-expected_debit)
        finally:
            await _cleanup(user_id)


async def test_a_correct_four_leg_request_still_builds(require_infra):
    """Control. The two-leg case would pass against a fix that only ever
    accepted exactly two strike arguments; an iron condor needs four."""
    with TestClient(app) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post(
                "/options/strategy",
                json=_base(
                    "iron_condor",
                    strategy_kwargs={
                        "put_long_strike": 24800,
                        "put_short_strike": 25000,
                        "call_short_strike": 25200,
                        "call_long_strike": 25400,
                    },
                ),
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert len(r.json()["legs"]) == 4
        finally:
            await _cleanup(user_id)


async def test_an_unknown_strategy_name_is_still_a_422(require_infra):
    """Control. That path already worked (a `ValueError` the route caught),
    and the new checks run before the builder is resolved -- so this pins
    that they did not swallow it or turn it into something else."""
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post("/options/strategy", json=_base("no_such_strategy"), headers=headers)
            assert r.status_code == 422, r.text
            assert "no_such_strategy" in r.text
        finally:
            await _cleanup(user_id)


async def test_a_strike_outside_the_chain_is_still_a_422(require_infra):
    """Control. The builders raise `KeyError`/`ValueError` for a strike the
    chain does not carry, and the route already turned those into 422s."""
    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post(
                "/options/strategy",
                json=_base(strategy_kwargs={"long_strike": 25000, "short_strike": 99999}),
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert "Traceback" not in r.text
        finally:
            await _cleanup(user_id)


# --- each layer, tested where the other cannot stand in for it ------------
#
# Injection found this, and it is the round-137 lesson in a new shape: the
# fix has two layers -- `build_strategy` rejecting bad kwargs with a
# `ValueError`, and the route catching any `TypeError` that still escapes --
# and every test above goes through the endpoint, where **either layer
# alone produces a 422**. Measured: removing `build_strategy`'s checks left
# the whole suite green (the route's guard caught the TypeError), and
# removing the route's guard left it green too (the checks ran first). Two
# layers that mask each other are two layers nothing is testing.


@pytest.mark.parametrize("strategy_name", ALL_STRATEGIES)
def test_build_strategy_itself_refuses_missing_arguments_as_a_value_error(strategy_name):
    """Behavioural proof for layer one, below the route.

    `TypeError` is what the bug raised and what the route now has to catch
    as a last resort; this function's own contract is to fail with a
    `ValueError` naming the argument instead, so the route never needs it.
    """
    with pytest.raises(ValueError) as exc:
        build_strategy(strategy_name, {25000.0: {"CALL": 100.0, "PUT": 100.0}})

    message = str(exc.value)
    for argument in required_arguments(strategy_name):
        assert argument in message, f"{strategy_name}: {message}"


def test_build_strategy_itself_refuses_an_unknown_argument_as_a_value_error():
    """Behavioural proof for layer one. `builder(chain, bogus=1)` raises
    `TypeError`; this must be caught before it gets there."""
    with pytest.raises(ValueError) as exc:
        build_strategy(
            "bull_call_spread",
            {25000.0: {"CALL": 100.0, "PUT": 100.0}, 25200.0: {"CALL": 80.0, "PUT": 120.0}},
            long_strike=25000.0,
            short_strike=25200.0,
            bogus=1,
        )
    assert "bogus" in str(exc.value)


def test_build_strategy_still_builds_when_the_arguments_are_right():
    """Control for the two proofs above, at the same level. Without it they
    would pass against a `build_strategy` that raised `ValueError`
    unconditionally."""
    legs = build_strategy(
        "bull_call_spread",
        {25000.0: {"CALL": 100.0, "PUT": 100.0}, 25200.0: {"CALL": 80.0, "PUT": 120.0}},
        long_strike=25000.0,
        short_strike=25200.0,
        quantity=1,
        lot_size=50,
    )
    assert len(legs) == 2


async def test_the_route_still_answers_422_if_a_builder_raises_typeerror(require_infra, monkeypatch):
    """Behavioural proof for layer two, reached the only way it can be.

    The route's `except TypeError` is unreachable while `build_strategy`'s
    own checks hold -- removing it changed nothing, which is what "untested
    defence" looks like. A builder whose signature has drifted under the
    validation is exactly the case it exists for, so that is what this
    simulates.
    """
    import app.api.options as options_module

    def _drifted(*args, **kwargs):
        raise TypeError("bull_call_spread() got an unexpected keyword argument 'long_strike'")

    monkeypatch.setattr(options_module, "build_strategy", _drifted)

    with TestClient(app, raise_server_exceptions=False) as client:
        headers, user_id = await _login(client)
        try:
            r = client.post("/options/strategy", json=_base(), headers=headers)
            assert r.status_code == 422, r.text
            assert "Traceback" not in r.text
            assert "bull_call_spread" in r.text
        finally:
            await _cleanup(user_id)
