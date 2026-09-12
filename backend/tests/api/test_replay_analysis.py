"""`GET /replay/{session_id}/analysis` -- structure as of the cursor.

`ReplayEngine.analyze` was written correctly and look-ahead-safely and
then never called: it had zero callers anywhere in `app/`. That left the
SMC/ICT stage of the blueprint §41 replay flow unreachable through the
API, and it left blueprint §45's look-ahead guarantee -- which the
blueprint calls mandatory -- protecting nothing, because
`ReplayClock.visible_candles` had exactly one consumer and that consumer
had none.

The endpoint is new surface, so the first two tests below are ordinary
coverage of it. The third is the one that matters: it asserts through the
API that the analysis never reflects a candle the cursor has not reached.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.replay import ReplayOrder, ReplaySession
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio

# A shape with real structure in it -- a rally, a break down, a deeper
# rally -- so swings, gaps and structure events actually appear rather
# than the engine returning empty containers that would make any
# assertion below vacuous.
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
# A tail that pushes decisively through the 130 equal-highs level the unit
# above keeps printing, so a liquidity sweep genuinely occurs partway
# through the replay rather than never. Without it `swept` is 0 at every
# cursor and any assertion about it is vacuous -- which is exactly what
# the first version of this fixture did.
_TAIL = [
    (128, 129, 126, 127),
    (127, 128, 125, 126),
    (126, 131, 126, 130),
    (130, 136, 129, 135),
    (135, 135, 131, 132),
    (132, 133, 129, 130),
    (130, 131, 127, 128),
    (128, 129, 126, 127),
    (127, 128, 125, 126),
    (126, 127, 124, 125),
]
_START = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)


async def _register(client: TestClient, label: str) -> tuple[str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return token, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _make_instrument() -> uuid.UUID:
    candles = [
        Candle(_START + timedelta(minutes=15 * i), o, h, low, c, 100)
        for i, (o, h, low, c) in enumerate(_UNIT * 3 + _TAIL)
    ]
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"RAN{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", candles)
        await db.commit()
        return instrument_id


async def _cleanup(user_ids: list[uuid.UUID], instrument_id: uuid.UUID) -> None:
    from app.database.models.market import Candle as CandleRow

    async with async_session_factory() as db:
        session_ids = (
            await db.execute(select(ReplaySession.id).where(ReplaySession.instrument_id == instrument_id))
        ).scalars().all()
        for session_id in session_ids:
            await db.execute(delete(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))
        await db.execute(delete(ReplaySession).where(ReplaySession.instrument_id == instrument_id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        for user_id in user_ids:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


def _timestamps(payload: dict) -> list[str]:
    """Every timestamp the analysis payload exposes, from any section.

    `liquidity_pools` is deliberately absent: the chart serialiser emits
    side, source, price, swept and rejected for a pool and no timestamp at
    all, so there is nothing here to bound. That section is checked by
    `_swept_count` below instead, because it carries the one piece of
    leak-prone state that a date bound would miss.
    """
    smc = payload["smc"]
    stamps = [event["timestamp"] for event in smc["structure_events"]]
    stamps += [gap["created_at"] for gap in smc["fair_value_gaps"]]
    stamps += [block["created_at"] for block in smc["order_blocks"]]
    return stamps


def _swept_count(payload: dict) -> int:
    """How many liquidity pools are marked swept.

    `detect_sweeps` decides this by scanning the candles *after* a pool
    forms, so it is exactly the field that a full-series leak inflates,
    and it is the one the timestamp bound cannot see.
    """
    return sum(1 for pool in payload["smc"]["liquidity_pools"] if pool["swept"])


async def test_the_analysis_endpoint_returns_structure_for_its_own_session(require_infra):
    """Ordinary coverage of new surface: the route exists, is reachable,
    and reports the cursor it analysed."""
    with TestClient(app) as client:
        token, user_id = await _register(client, "rananalysis")
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id = await _make_instrument()
        try:
            session_id = client.post(
                "/replay", json={"instrument_id": str(instrument_id), "timeframe": "15m"}, headers=headers
            ).json()["session_id"]
            client.post(f"/replay/{session_id}/step", params={"steps": 20}, headers=headers)

            r = client.get(f"/replay/{session_id}/analysis", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["cursor"] == 20
            assert set(body) == {"cursor", "as_of", "smc", "ict"}
            assert "structure_events" in body["smc"]
            assert "current_kill_zones" in body["ict"]
        finally:
            await _cleanup([user_id], instrument_id)


async def test_another_user_cannot_read_someone_elses_replay_analysis(require_infra):
    """A replay session is private state. The analysis route must go
    through the same ownership check as the rest of `/replay/*`, and
    answer 404 rather than 403 so it does not confirm the session exists.
    """
    with TestClient(app) as client:
        owner_token, owner_id = await _register(client, "ranowner")
        other_token, other_id = await _register(client, "ranintruder")
        instrument_id = await _make_instrument()
        try:
            session_id = client.post(
                "/replay",
                json={"instrument_id": str(instrument_id), "timeframe": "15m"},
                headers={"Authorization": f"Bearer {owner_token}"},
            ).json()["session_id"]

            r = client.get(
                f"/replay/{session_id}/analysis",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert r.status_code == 404, r.text
        finally:
            await _cleanup([owner_id, other_id], instrument_id)


async def test_the_analysis_never_reflects_a_candle_the_cursor_has_not_reached(require_infra):
    """Behavioural proof of blueprint §45 at the replay boundary.

    `tests/smc/test_reference_agreement.py` already proves the *detectors*
    never revise a past verdict. This proves the layer above it: that the
    endpoint hands them `clock.visible_candles` and not the full series.
    Those are different failures -- a perfectly look-ahead-safe detector
    fed the whole history leaks the future just as thoroughly -- and only
    this one is reachable by a user stepping through a replay.

    Every structure the payload carries is timestamped, so the assertion
    is direct: nothing may be dated after the candle the cursor sits on.
    The analysis is also required to grow as the cursor advances, which
    fails a stub that returns nothing and would otherwise satisfy the
    bound trivially.
    """
    with TestClient(app) as client:
        token, user_id = await _register(client, "ranlookahead")
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id = await _make_instrument()
        try:
            session_id = client.post(
                "/replay", json={"instrument_id": str(instrument_id), "timeframe": "15m"}, headers=headers
            ).json()["session_id"]

            seen_counts = []
            swept_seen = []
            cursor = 0
            for target in (12, 18, 24, 29, 33, 39):
                r = client.post(
                    f"/replay/{session_id}/step", params={"steps": target - cursor}, headers=headers
                )
                assert r.status_code == 200, r.text
                cursor = target

                r = client.get(f"/replay/{session_id}/analysis", headers=headers)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["cursor"] == cursor

                as_of = body["as_of"]
                assert as_of == (_START + timedelta(minutes=15 * cursor)).isoformat()

                future = [stamp for stamp in _timestamps(body) if stamp > as_of]
                assert not future, (
                    f"with the cursor on candle {cursor} ({as_of}) the analysis reported "
                    f"structure dated {sorted(set(future))} -- candles the replay has not "
                    "reached. The engine was handed more than clock.visible_candles."
                )

                swept_seen.append(_swept_count(body))
                seen_counts.append(len(_timestamps(body)))

            assert seen_counts[-1] > seen_counts[0], (
                "the analysis did not grow as the cursor advanced, so the look-ahead "
                f"bound above is satisfied trivially: {seen_counts}"
            )

            # The liquidity section carries no timestamp, so the date bound
            # above cannot see it -- and `swept` is precisely the field a
            # leak inflates, since `detect_sweeps` scans the candles *after*
            # a pool forms. The fixture's tail takes out the 130 equal-highs
            # level partway through, so the honest sequence starts at 0 and
            # ends above it. Analysing the full series would report the
            # sweep from the very first request.
            assert swept_seen[0] == 0, (
                f"a sweep was already reported at cursor 12, before the candle that "
                f"causes it: {swept_seen}"
            )
            assert swept_seen[-1] > 0, (
                f"no sweep was ever reported, so this assertion proves nothing: {swept_seen}"
            )
            assert swept_seen == sorted(swept_seen), (
                f"a sweep was withdrawn as the cursor advanced: {swept_seen}"
            )
        finally:
            await _cleanup([user_id], instrument_id)
