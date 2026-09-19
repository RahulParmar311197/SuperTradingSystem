"""Two concurrent refreshes with one token defeated the reuse detection.

`app/auth/service.py`'s `refresh` was read-check-write: SELECT the
session, check `revoked`, and much later -- after `get_user_by_id` and
`_issue_tokens` -- set `session.revoked = True`. Two concurrent requests
presenting the same refresh token both read `revoked=False`, both passed,
and both issued a fresh pair. Neither tripped the reuse detection, because
neither ever saw a revoked session.

Measured on the real ASGI app, two concurrent POSTs with one token:

    sequential:  200, 401 -> 2 sessions, 0 live, 1 reuse audit row
    concurrent:  200, 200 -> 3 sessions, 2 LIVE, 0 reuse audit rows

and repeated 20 times, the concurrent case violated the invariant in
**19 of 20 runs** before the fix and 0 of 20 after. That measured rate is
why the headline proof below repeats only three times: at a ~95% per-run
failure rate, three runs miss it with probability ~1e-4.

Why this one matters more than an ordinary race: round 84 added this
detection for exactly one scenario -- "a legitimate client and a thief
racing to use the same token". Racing was the case that slipped through.
A stolen refresh token used at the same moment as the legitimate one gave
the thief a live session family and raised no alarm at all.

The fix is a single conditional UPDATE that claims the rotation
atomically, so the winner is decided in the database rather than by two
in-process reads. That choice is deliberate: an `asyncio.Lock` like the
one round 152 added to `POST /orders` makes one process correct, and auth
has to hold across every replica.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select, text, update

from app.auth.security import TokenType, decode_token, decode_token_payload
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

REUSE_ACTION = "auth.refresh_token_reuse_detected"

# Every await in this file is bounded. A row-level claim that deadlocked
# would otherwise hang the suite reporting nothing (round 152's lesson).
_DEADLINE_SECONDS = 30


async def _bounded(awaitable, what: str):
    try:
        return await asyncio.wait_for(awaitable, timeout=_DEADLINE_SECONDS)
    except asyncio.TimeoutError:  # pragma: no cover - only on a regression
        raise AssertionError(f"{what} did not finish within {_DEADLINE_SECONDS}s") from None


async def _warm_the_connection_pool(n: int = 4) -> None:
    """Open `n` pooled connections before racing anything.

    Without this the race does not reproduce here at all, and the test
    below is vacuous: reverting `refresh` to its original read-check-write
    form left all nine tests green. The reason is pytest-specific.
    `tests/conftest.py` disposes the engine after every test (it has to --
    see its docstring), so each test starts with an EMPTY pool. The first
    of two concurrent requests takes a connection that is already open;
    the second has to establish a new one (TCP plus the Postgres auth
    handshake), which costs far more than the first request's whole
    transaction. The two never overlap, so nothing races.

    Measured on the original code, two concurrent refreshes repeated six
    times, varying only this:

        cold pool: [200, 401] once, then [200, 200] five times
        warm pool: [200, 200] six times

    -- the single [200, 401] is the very first iteration, the only one
    with a cold pool. A real server's pool is warm after its first
    requests, so warming is what reproduces production, not what distorts
    it.
    """

    async def touch() -> None:
        async with async_session_factory() as db:
            await db.execute(text("SELECT pg_sleep(0.05)"))

    await _bounded(asyncio.gather(*[touch() for _ in range(n)]), "warming the connection pool")


async def _cleanup(user_id: uuid.UUID) -> None:
    """Child rows first -- audit_logs and sessions both reference the user."""
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _sessions_and_reuse_audits(user_id: uuid.UUID):
    async with async_session_factory() as db:
        sessions = (
            await db.execute(select(UserSession).where(UserSession.user_id == user_id))
        ).scalars().all()
        audits = (
            await db.execute(
                select(AuditLog).where(AuditLog.user_id == user_id, AuditLog.action == REUSE_ACTION)
            )
        ).scalars().all()
    return sessions, audits


class _Account:
    """A registered, logged-in user and its first refresh token."""

    def __init__(self, client, user_id, refresh_token, session_id):
        self.client = client
        self.user_id = user_id
        self.refresh_token = refresh_token
        self.session_id = session_id

    def refresh(self, token=None):
        return self.client.post("/auth/refresh", json={"refresh_token": token or self.refresh_token})


async def _account(client: httpx.AsyncClient) -> _Account:
    email = f"refresh-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "RT"})
    assert r.status_code == 201, r.text
    body = (await client.post("/auth/login", json={"email": email, "password": "testpass123"})).json()
    return _Account(
        client,
        uuid.UUID(decode_token(body["access_token"], TokenType.ACCESS)),
        body["refresh_token"],
        uuid.UUID(decode_token_payload(body["refresh_token"], TokenType.REFRESH)["sid"]),
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- the finding ----------------------------------------------------------


@pytest.mark.parametrize("repeat", range(3))
async def test_two_concurrent_refreshes_of_one_token_leave_no_live_session(repeat, require_infra):
    """Behavioural proof. Exactly one of the two may succeed, and the loser
    must be treated as reuse -- which revokes the whole family and writes
    the audit row. Before the fix both succeeded and left two live
    sessions with no audit row at all."""
    async with _client() as client:
        account = await _account(client)
        try:
            await _warm_the_connection_pool()
            responses = await _bounded(
                asyncio.gather(account.refresh(), account.refresh()), "two concurrent refreshes"
            )
            codes = sorted(r.status_code for r in responses)
            assert codes == [200, 401], codes

            sessions, audits = await _sessions_and_reuse_audits(account.user_id)
            live = [s for s in sessions if not s.revoked]
            assert live == [], f"{len(live)} session(s) still live after a detected token reuse"
            assert len(audits) == 1, f"expected exactly one {REUSE_ACTION} row, got {len(audits)}"
        finally:
            await _cleanup(account.user_id)


async def test_the_sequential_answer_and_the_concurrent_answer_agree(require_infra):
    """Behavioural proof that the fix restores the intended semantics
    rather than merely some other outcome. Two accounts, the same two
    calls; one makes them one at a time, the other together."""
    async with _client() as client:
        sequential = await _account(client)
        concurrent = await _account(client)
        try:
            await _warm_the_connection_pool()
            seq_codes = sorted(
                [
                    (await _bounded(sequential.refresh(), "sequential refresh 1")).status_code,
                    (await _bounded(sequential.refresh(), "sequential refresh 2")).status_code,
                ]
            )
            con_codes = sorted(
                r.status_code
                for r in await _bounded(
                    asyncio.gather(concurrent.refresh(), concurrent.refresh()), "concurrent refreshes"
                )
            )
            assert seq_codes == con_codes == [200, 401], (seq_codes, con_codes)

            for account in (sequential, concurrent):
                sessions, audits = await _sessions_and_reuse_audits(account.user_id)
                assert [s for s in sessions if not s.revoked] == []
                assert len(audits) == 1
        finally:
            await _cleanup(sequential.user_id)
            await _cleanup(concurrent.user_id)


# --- what the fix must not break -----------------------------------------


async def test_an_ordinary_refresh_still_rotates(require_infra):
    """Control with the state pinned. A change that refused everything, or
    one that issued a pair without revoking the old session, would both
    satisfy the proofs above."""
    async with _client() as client:
        account = await _account(client)
        try:
            r = await _bounded(account.refresh(), "refresh")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["refresh_token"] != account.refresh_token, "the token must actually rotate"

            sessions, audits = await _sessions_and_reuse_audits(account.user_id)
            assert len(sessions) == 2, sessions
            assert len([s for s in sessions if s.revoked]) == 1
            assert len([s for s in sessions if not s.revoked]) == 1
            assert audits == [], "an ordinary rotation is not a reuse incident"
        finally:
            await _cleanup(account.user_id)


async def test_a_chain_of_rotations_keeps_working(require_infra):
    """Control. Each new token must itself be refreshable -- a fix that
    revoked the session it had just issued would pass the single-rotation
    test and break every client on its second refresh."""
    async with _client() as client:
        account = await _account(client)
        try:
            token = account.refresh_token
            for step in range(3):
                r = await _bounded(account.refresh(token), f"rotation {step}")
                assert r.status_code == 200, f"rotation {step}: {r.text}"
                token = r.json()["refresh_token"]

            sessions, audits = await _sessions_and_reuse_audits(account.user_id)
            assert len([s for s in sessions if not s.revoked]) == 1, sessions
            assert audits == []
        finally:
            await _cleanup(account.user_id)


async def test_a_wrong_token_naming_a_real_session_does_not_revoke_it(require_infra):
    """Control, and the guard against the tempting wrong fix.

    Claiming the rotation by session id alone -- revoking first and
    verifying the hash afterwards -- would let anyone who learns a session
    id log that session out. The hash is part of the claim's WHERE clause
    precisely so this cannot happen, and this test is what notices if it
    ever moves.
    """
    async with _client() as client:
        victim = await _account(client)
        attacker = await _account(client)
        try:
            # A syntactically valid refresh token for the victim's session
            # id, signed correctly, but not the one the session stores.
            from app.auth.security import create_refresh_token

            forged = create_refresh_token(victim.user_id, victim.session_id)
            assert forged != victim.refresh_token

            r = await _bounded(victim.refresh(forged), "forged refresh")
            assert r.status_code == 401, r.text

            sessions, audits = await _sessions_and_reuse_audits(victim.user_id)
            assert [s.revoked for s in sessions] == [False], "the forged token revoked a real session"
            assert audits == [], "a forged token is not the victim's own reuse"

            # And the victim's real token still works afterwards.
            assert (await _bounded(victim.refresh(), "victim refresh")).status_code == 200
        finally:
            await _cleanup(victim.user_id)
            await _cleanup(attacker.user_id)


@pytest.mark.parametrize("repeat", range(3))
async def test_a_forged_token_racing_the_real_one_cannot_lock_the_victim_out(repeat, require_infra):
    """The concurrent half of the control above.

    This was written expecting to show that the hash in the claim's WHERE
    clause is load-bearing -- that without it the forged request wins the
    claim, the victim's own refresh finds the row already revoked, reads
    that as reuse, and revokes the whole family. It does not. Measured with
    the predicate removed, four runs, and again with it in place:

        forged=401 real=200 sessions=2 live=1 reuse_audits=0   (both)

    identical. Postgres row-locks the two claims, so the forged UPDATE
    holds the row until its request ends; every path that refuses ends by
    raising, which rolls the claim back; the real refresh's UPDATE then
    re-evaluates against an unrevoked row and wins. The rollback, not the
    predicate, is what protects the victim today.

    The predicate is kept anyway, and this is why: injecting it away
    *together* with a commit of the claim before the refusal checks fails
    all four forged-token assertions (both this test and the sequential one
    above), where either injection alone leaves them green. Claiming a
    rotation on the session id alone is only safe while nothing between the
    claim and the refusal ever commits -- an invisible property of code
    some distance away. Claiming on the hash is safe on its own terms.
    """
    async with _client() as client:
        victim = await _account(client)
        try:
            from app.auth.security import create_refresh_token

            forged = create_refresh_token(victim.user_id, victim.session_id)
            assert forged != victim.refresh_token

            await _warm_the_connection_pool()
            forged_response, real_response = await _bounded(
                asyncio.gather(victim.refresh(forged), victim.refresh()), "forged racing the real token"
            )
            assert forged_response.status_code == 401, forged_response.text
            assert real_response.status_code == 200, real_response.text

            sessions, audits = await _sessions_and_reuse_audits(victim.user_id)
            assert len([s for s in sessions if not s.revoked]) == 1, sessions
            assert audits == [], "a forged token locked the victim out of every session"
        finally:
            await _cleanup(victim.user_id)

async def test_an_expired_token_is_refused_without_revoking_or_false_alarming(require_infra):
    """Control, and a measured non-regression.

    The claiming UPDATE runs before the expiry check, so an expired token
    momentarily marks its session revoked -- but that write rides the
    request's transaction, which is discarded when the expiry check
    raises. Nothing is durably revoked and no incident is logged, so
    replaying a merely-expired token never looks like theft. This was
    predicted to regress, measured, and found not to; the test exists so a
    refactor that commits earlier cannot introduce the false alarm
    silently.
    """
    async with _client() as client:
        account = await _account(client)
        try:
            async with async_session_factory() as db:
                await db.execute(
                    update(UserSession)
                    .where(UserSession.id == account.session_id)
                    .values(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
                )
                await db.commit()

            for attempt in range(2):
                r = await _bounded(account.refresh(), f"expired refresh {attempt}")
                assert r.status_code == 401, r.text
                sessions, audits = await _sessions_and_reuse_audits(account.user_id)
                assert [s.revoked for s in sessions] == [False], f"attempt {attempt}: session was revoked"
                assert audits == [], f"attempt {attempt}: an expired token raised a reuse alarm"
        finally:
            await _cleanup(account.user_id)


async def test_the_ordinary_sequential_replay_still_trips_the_detection(require_infra):
    """Control for round 84's own behaviour, which this change must
    preserve: presenting an already-rotated token revokes the whole family
    and logs the incident."""
    async with _client() as client:
        account = await _account(client)
        try:
            first = await _bounded(account.refresh(), "first refresh")
            assert first.status_code == 200, first.text
            rotated_to = first.json()["refresh_token"]

            replay = await _bounded(account.refresh(), "replay of the used token")
            assert replay.status_code == 401, replay.text

            sessions, audits = await _sessions_and_reuse_audits(account.user_id)
            assert [s for s in sessions if not s.revoked] == [], "the family must be revoked"
            assert len(audits) == 1, audits

            # And the session the rotation produced is cut off too, which is
            # the whole point of revoking the family rather than one row.
            after = await _bounded(account.refresh(rotated_to), "refresh with the rotated-to token")
            assert after.status_code == 401, after.text
        finally:
            await _cleanup(account.user_id)
