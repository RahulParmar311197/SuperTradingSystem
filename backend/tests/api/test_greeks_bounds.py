"""`POST /options/greeks` answered 500 on four extreme inputs.

Black-Scholes is pure arithmetic with no persistence behind it, so the harm
is a 500 rather than a wrong number in a journal — but a 500 is still the
wrong answer for a bad request, and it reaches the client as a traceback
naming nothing useful. Measured against the live endpoint:

    rate=1e308                  -> 500  "Out of range float values are not
                                         JSON compliant" (the result is inf)
    rate=-1e308                 -> 500  OverflowError: math range error
    time_to_expiry_years=1e308  -> 500  JSON-non-compliant
    iv=1e308                    -> 500  OverflowError (34, numerical result
                                         out of range)

`_d1_d2` already refuses zero and negative spot/strike/time/iv with a
`ValueError` the route turns into a 422, so only the upper end was open —
plus `rate`, the one field that may legitimately be negative, which had no
guard at either end.

Same family as the `POST /instruments` numerics (round 144) and the
`cost_model` shapes (round 139), and the last entry on that sweep's list.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

BASE = {
    "spot": 25000.0,
    "strike": 25000.0,
    "time_to_expiry_years": 0.05,
    "rate": 0.06,
    "iv": 0.2,
    "option_type": "CALL",
}


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"greeks-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Greeks"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _cleanup(user_id: uuid.UUID) -> None:
    """Child rows first — audit_logs and sessions both reference the user."""
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# Each of these was measured as a 500 before the bounds existed.
WAS_A_500 = [
    ("rate", 1e308, "JSON-non-compliant inf"),
    ("rate", -1e308, "OverflowError: math range error"),
    ("time_to_expiry_years", 1e308, "JSON-non-compliant"),
    ("iv", 1e308, "OverflowError"),
]


@pytest.mark.parametrize("field,value,before", WAS_A_500, ids=[f"{f}={v}" for f, v, _ in WAS_A_500])
async def test_an_input_that_used_to_500_is_a_422_naming_the_field(field, value, before, require_infra):
    """Behavioural proof. The status code is half of it; the other half is
    that the answer says which field was wrong, which a traceback did not."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/greeks", json={**BASE, field: value}, headers=headers)
            assert r.status_code == 422, f"{field}={value} was a 500 ({before}); got {r.status_code}: {r.text[:200]}"
            assert any(field in str(d.get("loc", "")) for d in r.json()["detail"]), r.text[:300]
        finally:
            await _cleanup(user_id)


ACCEPTED = [
    ("spot", 1e9),
    ("strike", 0.05),
    ("time_to_expiry_years", 100.0),  # the ceiling itself
    ("time_to_expiry_years", 3.0),    # a real LEAPS
    ("iv", 100.0),                   # the ceiling itself: 10,000% vol
    ("rate", -1.0),                  # a deeply negative policy rate
    ("rate", 0.0),
    ("rate", 1.0),
]


@pytest.mark.parametrize("field,value", ACCEPTED, ids=[f"{f}={v}" for f, v in ACCEPTED])
async def test_a_legitimate_extreme_is_still_priced(field, value, require_infra):
    """Control against over-tightening. Every ceiling is exercised at its own
    edge, and `rate` at both ends — it is a decimal fraction, so a negative
    one is a real policy rate rather than a mistake."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/greeks", json={**BASE, field: value}, headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert all(k in body for k in ("price", "delta", "gamma", "theta", "vega", "rho")), body
        finally:
            await _cleanup(user_id)


async def test_an_ordinary_call_is_unchanged(require_infra):
    """Control, with the numbers pinned. A bound that quietly altered an
    input would pass every status-code assertion above."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/greeks", json=BASE, headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            # An at-the-money call, 0.05 years out at 20% vol. Derived from
            # this fixture by running it, not copied from elsewhere.
            assert body["price"] == pytest.approx(483.77, abs=0.01)
            assert body["delta"] == pytest.approx(0.5356, abs=0.0001)
        finally:
            await _cleanup(user_id)


SPOT_AND_STRIKE_EXTREMES = [
    ("spot", 1e308, 1e308),
    ("spot", 1e-308, 0.0),
    ("strike", 1e308, 0.0),
    ("strike", 1e-308, 25000.0),
]


@pytest.mark.parametrize(
    "field,value,expected_price", SPOT_AND_STRIKE_EXTREMES,
    ids=[f"{f}={v}" for f, v, _ in SPOT_AND_STRIKE_EXTREMES],
)
async def test_spot_and_strike_are_deliberately_not_capped(field, value, expected_price, require_infra):
    """Control, and a deliberate non-fix.

    A price ceiling was written into this fix first and then removed:
    injection showed the suite stayed green without it, because nothing it
    prevented had ever failed. Every extreme `spot` and `strike` accept
    answers correctly, and the expected prices here are what the endpoint
    actually returns — so this test fails if a ceiling is ever added back
    without a measured reason.
    """
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/greeks", json={**BASE, field: value}, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["price"] == pytest.approx(expected_price), r.json()
        finally:
            await _cleanup(user_id)


async def test_a_vanishingly_short_expiry_is_still_answered(require_infra):
    """Control, and a deliberate non-fix. An option expiring in a
    microsecond really does have enormous gamma, so the huge number 1e-300
    produces is arithmetic answering an absurd question correctly. Adding a
    floor would be this endpoint refusing a calculation it can do — and
    would silently change what `_d1_d2`'s own `> 0` guard means."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/options/greeks", json={**BASE, "time_to_expiry_years": 1e-300}, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["gamma"] > 1e100, r.json()["gamma"]
        finally:
            await _cleanup(user_id)


async def test_the_lower_guards_still_answer_422(require_infra):
    """Control for the half of this that already worked. `_d1_d2` refuses
    zero and negative spot/strike/time/iv, and those must keep answering
    422 rather than becoming a 500 by way of the new bounds."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            for field in ("spot", "strike", "time_to_expiry_years", "iv"):
                for value in (0.0, -1.0):
                    r = client.post("/options/greeks", json={**BASE, field: value}, headers=headers)
                    assert r.status_code == 422, f"{field}={value}: {r.status_code} {r.text[:150]}"
        finally:
            await _cleanup(user_id)
