"""`GET /health` reported both brokers as a constant.

`app/monitoring/health.py` computed `database`, `redis` and `workers` by
probing, and derived `ai` from settings -- but `dhan` and `upstox` were
the literal `ComponentStatus.NOT_CONFIGURED.value`, written into the dict
and never computed from anything. Measured against the real app:

    nothing configured at all   upstox NOT_CONFIGURED   ai NOT_CONFIGURED
    credentials set in settings upstox NOT_CONFIGURED   ai HEALTHY
    one ACTIVE UPSTOX account   upstox NOT_CONFIGURED
      (resolve_broker -> UpstoxBroker, so that user's orders go LIVE)

The middle line is the clearest proof it was a constant: `ai` on the very
same response flips and these two never did. The last line is the harm.
`/health` is public and unauthenticated, and round 125 -- which stopped
this endpoint 500ing during a Redis outage -- called it "the one endpoint
you consult during an outage". It reported no broker connected while real
orders were routing to Upstox: a monitoring surface wrong in the
direction that hides a live venue.

WHY NOT DERIVED FROM `settings`, which would have been the exact parallel
of the `ai` line. `upstox_client_id`/`upstox_secret` gate only the OAuth
routes (`GET /brokers/upstox/authorize`, `/callback`).
`POST /brokers/connect` takes credentials directly and never reads them,
so an account can be ACTIVE and placing live orders on a deployment where
those settings are unset -- which is exactly the measured case. A
settings-derived answer would have left it reporting NOT_CONFIGURED, so
the obvious fix would have fixed nothing. `test_a_live_broker_is_visible_
even_with_no_oauth_settings_at_all` pins that.
"""

import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.core.encryption import encrypt_credentials
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User
from app.database.session import async_session_factory
from app.main import app
from app.monitoring.health import ComponentStatus, _status_for, check_brokers


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


async def _account(broker: BrokerName, status: BrokerAccountStatus) -> uuid.UUID:
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"health-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("testpass123"),
            name="Health Broker",
        )
        db.add(user)
        await db.flush()
        db.add(
            BrokerAccount(
                id=uuid.uuid4(),
                user_id=user.id,
                broker=broker,
                encrypted_credentials=encrypt_credentials({"access_token": "test-token"}),
                status=status,
            )
        )
        await db.commit()
        return user.id


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(BrokerAccount).where(BrokerAccount.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _require_no_rows_for(broker: BrokerName) -> None:
    """An explicit precondition rather than a silent assumption.

    `check_brokers` is deployment-wide by design, and the suite shares one
    database -- there are leftover PAPER accounts in it right now from
    earlier runs. A test asserting NOT_CONFIGURED or DOWN is only
    meaningful if nothing else has left a row for this broker, so it says
    so and fails loudly instead of flaking.
    """
    async with async_session_factory() as db:
        rows = (await db.execute(select(BrokerAccount.id).where(BrokerAccount.broker == broker))).all()
    assert not rows, f"{broker.value} rows left over by another test ({len(rows)}); this test cannot judge"


# --- the finding ----------------------------------------------------------


async def test_an_active_account_is_not_reported_as_not_configured(require_infra):
    user_id = await _account(BrokerName.UPSTOX, BrokerAccountStatus.ACTIVE)
    try:
        async with _client() as client:
            body = (await client.get("/health")).json()
        assert body["upstox"] == ComponentStatus.HEALTHY.value, body
    finally:
        await _cleanup(user_id)


async def test_a_live_broker_is_visible_even_with_no_oauth_settings_at_all(require_infra, monkeypatch):
    """The measured case, and the reason this is not derived from settings.

    `POST /brokers/connect` never reads `upstox_client_id`/`upstox_secret`,
    so an account is ACTIVE and routing live orders with both unset."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "upstox_client_id", None)
    monkeypatch.setattr(settings, "upstox_secret", None)

    user_id = await _account(BrokerName.UPSTOX, BrokerAccountStatus.ACTIVE)
    try:
        async with _client() as client:
            body = (await client.get("/health")).json()
        assert body["upstox"] == ComponentStatus.HEALTHY.value, body
    finally:
        await _cleanup(user_id)


async def test_the_two_brokers_are_reported_independently(require_infra):
    """Non-vacuity, and the over-fix control: connecting Upstox must not
    make Dhan claim to be connected too."""
    await _require_no_rows_for(BrokerName.DHAN)
    user_id = await _account(BrokerName.UPSTOX, BrokerAccountStatus.ACTIVE)
    try:
        async with _client() as client:
            body = (await client.get("/health")).json()
        assert body["upstox"] == ComponentStatus.HEALTHY.value, body
        assert body["dhan"] == ComponentStatus.NOT_CONFIGURED.value, body
    finally:
        await _cleanup(user_id)


async def test_an_account_that_is_no_longer_active_reads_down(require_infra):
    """DOWN, not NOT_CONFIGURED: this broker WAS set up and cannot be used
    now, which is the state worth paging on. Reporting it as never
    configured would hide a broker that stopped working."""
    await _require_no_rows_for(BrokerName.DHAN)
    user_id = await _account(BrokerName.DHAN, BrokerAccountStatus.ERROR)
    try:
        assert (await check_brokers())["dhan"] == ComponentStatus.DOWN.value
    finally:
        await _cleanup(user_id)


async def test_a_broker_nobody_connected_still_reads_not_configured(require_infra):
    """The honest answer when there is genuinely nothing there -- the one
    case the old constant got right, which must survive the fix."""
    await _require_no_rows_for(BrokerName.DHAN)
    assert (await check_brokers())["dhan"] == ComponentStatus.NOT_CONFIGURED.value


@pytest.mark.parametrize(
    "statuses, expected",
    [
        ([], ComponentStatus.NOT_CONFIGURED),
        ([BrokerAccountStatus.ACTIVE], ComponentStatus.HEALTHY),
        ([BrokerAccountStatus.ERROR], ComponentStatus.DOWN),
        ([BrokerAccountStatus.DISCONNECTED], ComponentStatus.DOWN),
        ([BrokerAccountStatus.DISCONNECTED, BrokerAccountStatus.ACTIVE], ComponentStatus.HEALTHY),
        ([BrokerAccountStatus.DISCONNECTED, BrokerAccountStatus.ERROR], ComponentStatus.DOWN),
    ],
)
def test_the_mapping_from_accounts_to_a_component_status(statuses, expected):
    """The mapping itself, with no database in the way -- including the
    mixed case, where one usable account is enough to call the broker
    usable however many dead ones sit beside it."""
    assert _status_for(statuses) is expected


# --- the risk the new database read introduces, not the one it removes ----


async def test_health_still_answers_when_the_database_is_unreachable(require_infra, monkeypatch):
    """Round 125's guarantee, which this change must not undo: `/health` is
    what an operator opens during an outage, so a database it cannot read
    must degrade to DOWN rather than take the endpoint down."""
    import app.monitoring.health as health_module

    def _explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(health_module, "async_session_factory", _explode)

    async with _client() as client:
        response = await client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dhan"] == ComponentStatus.DOWN.value, body
    assert body["upstox"] == ComponentStatus.DOWN.value, body
    assert body["api"] == ComponentStatus.HEALTHY.value, body
