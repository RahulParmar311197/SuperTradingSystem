"""Two concurrent edits of one strategy escaped as a 500, and one of them
vanished.

`PUT /strategies/{id}` is read-modify-write across an await:

    row = await db.get(StrategyRow, strategy_id)   # reads version
    row.version += 1                               # computes the next one
    await _snapshot_version(db, row)                # INSERT into
    await db.commit()                               #   strategy_versions

and `strategy_versions` carries
`UniqueConstraint("strategy_id", "version")` (app/database/models/strategy.py).
Unlocked, two concurrent edits of one strategy both read version 1 and
both computed 2. Measured against the real ASGI app, holding the first
request open between its read and its commit:

    versions computed by each request: [2, 2]
    put[0]: 500 Traceback (most recent call last):
    put[1]: 200 {... "name":"Edit B","version":2 ...}
    strategy_versions: [(1, 'Bullish ...'), (2, 'Edit B')]

Two distinct harms. One editor got a **500 with a raw traceback** for a
request that was entirely valid. And their edit was **lost without a
trace** -- no row, no version, nothing in the history blueprint §91 exists
to keep, so "a trade's strategy_version resolves back to the exact DSL
that produced it" quietly stopped being true for the definition that
briefly won the race.

The fix locks the strategy row (`with_for_update=True`), so the second
reader waits and reads the version the first one actually wrote. Both
edits then land, as versions 2 and 3.

WHY THE INTERLEAVE IS FORCED WITH AN EVENT, NOT A SLEEP (round 166's
lesson, on the same shape). Fired as a plain `asyncio.gather` of two PUTs,
this does not reproduce at all: the handler's only awaits are quick local
round trips, so request 0 ran its whole critical section before request 1
started, and the probe reported a clean [2, 3] -- a false negative. The
gate below blocks request 0 inside `_snapshot_version`, after it has
computed its version and before it commits, which is exactly the window.
The test is then deterministic in both the fixed and the broken world.
"""

import asyncio
import uuid

import httpx
from sqlalchemy import delete, select

from app.api import strategies as strategies_module
from app.auth.security import hash_password
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

_DEADLINE = 30


def _client() -> httpx.AsyncClient:
    # `raise_app_exceptions=False` so an unhandled error arrives as the 500
    # a real client would see, rather than as an exception in the test.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


async def _user(client) -> tuple[uuid.UUID, dict]:
    email = f"sedit-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"), name="Edit Race")
        db.add(row)
        await db.commit()
        user_id = row.id
    r = await client.post("/auth/login", json={"email": email, "password": "testpass123"})
    assert r.status_code == 200, r.text
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _strategy(client, headers) -> tuple[str, dict]:
    r = await client.post("/strategies/library/bullish_liquidity_sweep", headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1, body
    return body["id"], body["definition"]


class _Gate:
    """Holds the FIRST editor inside the window and records, in order, the
    version each request computed for itself."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.versions: list[int] = []
        self._original = strategies_module._snapshot_version

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(strategies_module, "_snapshot_version", self)

    async def __call__(self, db, row) -> None:
        self.versions.append(row.version)
        if len(self.versions) == 1:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), timeout=_DEADLINE)
        await self._original(db, row)


async def _race(client, headers, strategy_id: str, definition: dict, gate: _Gate, monkeypatch) -> list:
    """Fire two edits with the first one held open inside the window.

    Installs the gate itself, deliberately: `install_library_strategy`
    calls `_snapshot_version` too, so a gate armed any earlier catches the
    setup call instead of the first edit and the test never reaches the
    race at all.
    """
    gate.install(monkeypatch)
    a = dict(definition) | {"name": "Edit A"}
    b = dict(definition) | {"name": "Edit B"}

    first = asyncio.create_task(client.put(f"/strategies/{strategy_id}", json=a, headers=headers))
    await asyncio.wait_for(gate.entered.wait(), timeout=_DEADLINE)
    second = asyncio.create_task(client.put(f"/strategies/{strategy_id}", json=b, headers=headers))
    # Long enough for request 1 to reach its own read and either pass it
    # (broken) or block on the row lock (fixed) -- it is not a timing
    # assumption, only a chance to get there before the gate opens.
    await asyncio.sleep(0.3)
    gate.release.set()
    return await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=_DEADLINE)


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        strategy_ids = (await db.execute(select(StrategyRow.id).where(StrategyRow.user_id == user_id))).scalars().all()
        if strategy_ids:
            await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id.in_(strategy_ids)))
        for model in (StrategyRow, AuditLog, UserSession):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# --- the finding ----------------------------------------------------------


async def test_two_concurrent_edits_do_not_escape_as_a_500(require_infra, monkeypatch):
    gate = _Gate()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            strategy_id, definition = await _strategy(client, headers)
            results = await _race(client, headers, strategy_id, definition, gate, monkeypatch)

            raised = [r for r in results if isinstance(r, BaseException)]
            assert not raised, f"an edit escaped as an exception: {raised!r}"
            codes = sorted(r.status_code for r in results)
            assert codes == [200, 200], f"a concurrent edit failed: {codes}"
        finally:
            await _cleanup(user_id)


async def test_neither_concurrent_edit_is_lost_from_the_history(require_infra, monkeypatch):
    """The second harm, and the one §91 is about: the losing edit used to
    leave no row at all. Both must be snapshotted, under their own names."""
    gate = _Gate()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            strategy_id, definition = await _strategy(client, headers)
            await _race(client, headers, strategy_id, definition, gate, monkeypatch)

            async with async_session_factory() as db:
                rows = (
                    await db.execute(
                        select(StrategyVersionRow.version, StrategyVersionRow.name)
                        .where(StrategyVersionRow.strategy_id == uuid.UUID(strategy_id))
                        .order_by(StrategyVersionRow.version)
                    )
                ).all()
            assert [v for v, _ in rows] == [1, 2, 3], f"versions snapshotted: {rows}"
            assert {name for _, name in rows} == {
                "Bullish Liquidity Sweep",
                "Edit A",
                "Edit B",
            }, f"an edit is missing from the history: {rows}"
        finally:
            await _cleanup(user_id)


async def test_the_second_editor_reads_the_version_the_first_one_wrote(require_infra, monkeypatch):
    """Non-vacuity for the two above, and the mechanism itself. Under the
    bug both requests computed 2 (`[2, 2]`) and the unique constraint then
    decided which one died. Locked, the second reads 2 and computes 3."""
    gate = _Gate()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            strategy_id, definition = await _strategy(client, headers)
            await _race(client, headers, strategy_id, definition, gate, monkeypatch)

            assert gate.versions == [2, 3], (
                f"two concurrent edits computed {gate.versions}; a repeated version is the race"
            )
        finally:
            await _cleanup(user_id)


# --- the risk the lock introduces, not the one it removes ------------------


async def test_an_ordinary_sequential_edit_still_bumps_by_one(require_infra):
    """No gate, no race: the lock must not have changed the plain path."""
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            strategy_id, definition = await _strategy(client, headers)
            for expected, label in ((2, "First edit"), (3, "Second edit")):
                r = await client.put(
                    f"/strategies/{strategy_id}", json=dict(definition) | {"name": label}, headers=headers
                )
                assert r.status_code == 200, r.text
                assert r.json()["version"] == expected, r.json()
                assert r.json()["name"] == label, r.json()
        finally:
            await _cleanup(user_id)


async def test_a_non_owner_gets_404_and_does_not_hold_the_row_locked(require_infra):
    """The row is now locked BEFORE the ownership check, so a stranger's
    404 takes a lock on someone else's strategy. It must be released when
    the handler raises -- otherwise one unauthorised request would wedge
    every subsequent edit of that strategy until the connection died.

    The second half of this is non-vacuous: holding the same row locked
    from outside the app (`SELECT ... FOR UPDATE`, uncommitted) and then
    issuing the owner's edit, the request blocked until the 5s deadline
    rather than answering -- measured. So "the owner can still edit,
    promptly" does fail when the lock is retained.
    """
    async with _client() as client:
        owner_id, owner_headers = await _user(client)
        stranger_id, stranger_headers = await _user(client)
        try:
            strategy_id, definition = await _strategy(client, owner_headers)

            r = await client.put(
                f"/strategies/{strategy_id}", json=dict(definition) | {"name": "Not yours"}, headers=stranger_headers
            )
            assert r.status_code == 404, r.text

            # The owner must still be able to edit, promptly.
            r = await asyncio.wait_for(
                client.put(
                    f"/strategies/{strategy_id}", json=dict(definition) | {"name": "Mine"}, headers=owner_headers
                ),
                timeout=_DEADLINE,
            )
            assert r.status_code == 200, r.text
            assert r.json()["version"] == 2, r.json()
        finally:
            await _cleanup(stranger_id)
            await _cleanup(owner_id)
