"""Resolves the `Broker` instance for a user's trading stack (blueprint
§50, §53 "Broker Abstraction" — the strategy/risk/execution layers never
branch on which broker is connected, they just see `Broker`).

This is the piece blueprint §8/§9 describe as still missing even after a
real Upstox adapter exists: *something* has to pick that adapter for a
*specific user's* orders instead of every account always trading against
`MockBroker`. A user with no connected broker account trades against
`MockBroker` — that is Stage 9's honest default, not a workaround. A user
with an ACTIVE Upstox `BrokerAccount` gets a real `UpstoxBroker` built
from their stored (decrypted) OAuth token; this is the first point in the
codebase where a specific user's orders would actually reach a real
broker, once that stored token is valid — still unverified against
Upstox's live servers in this environment (see docs/ARCHITECTURE.md).

Deliberately does NOT catch a broken connection (missing/malformed stored
credentials, or Dhan's still-`NotImplementedError` adapter) and silently
fall back to `MockBroker` — blueprint §101: "Never make paper and live
look identical." A user who connected a broker and gets an error knows
something is wrong; silently paper-trading their "live" order would hide
that.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import Broker, BrokerError
from app.brokers.mock import MockBroker
from app.brokers.upstox.adapter import UpstoxBroker
from app.core.encryption import decrypt_credentials
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User


async def resolve_broker(db: AsyncSession, user: User) -> tuple[Broker, uuid.UUID | None]:
    """Returns the resolved adapter alongside the `BrokerAccount.id` it was
    built from (`None` for `MockBroker`, since there's no connected account
    to attribute a paper trade to) -- `Order.broker_account_id` exists
    specifically so a placed order can be traced back to which of a user's
    (possibly several, over time) connected broker accounts executed it;
    without returning it here, no caller could ever populate that column.
    """
    stmt = (
        select(BrokerAccount)
        .where(BrokerAccount.user_id == user.id, BrokerAccount.status == BrokerAccountStatus.ACTIVE)
        .order_by(BrokerAccount.created_at.desc())
    )
    account = (await db.execute(stmt)).scalars().first()
    if account is None:
        return MockBroker(starting_balance=100_000.0), None

    credentials = decrypt_credentials(account.encrypted_credentials)

    if account.broker == BrokerName.UPSTOX:
        access_token = credentials.get("access_token")
        if not access_token:
            raise BrokerError(f"Connected Upstox account {account.id} has no access_token stored")
        return UpstoxBroker(access_token=access_token), account.id

    if account.broker == BrokerName.DHAN:
        # `DhanBroker` is a documented skeleton: every method of it raises
        # `NotImplementedError` (see its module docstring and blueprint
        # §51/§120 -- it must be written against Dhan's *current* official
        # API, not a guess). Handing one back meant the failure surfaced
        # several frames deeper as a raw `NotImplementedError`: measured,
        # `POST /orders` for a user with a connected Dhan account came back
        # 500 with "TODO: implement using Dhan's market quote / LTP
        # endpoint", every time, with nothing telling them their broker is
        # the reason.
        #
        # Refusing here keeps this module's existing rule -- never silently
        # fall back to `MockBroker`, blueprint §101 "never make paper and
        # live look identical" -- while making the refusal legible.
        # `POST /brokers/connect` now turns DHAN away at the door, so this
        # covers accounts stored before that guard existed.
        raise BrokerError(
            f"Connected Dhan account {account.id} cannot trade: the Dhan adapter is not "
            "implemented yet (app/brokers/dhan/adapter.py). Disconnect it, or connect an "
            "Upstox account, to place orders."
        )

    # BrokerName.PAPER — an explicit paper-mode "connection" trades mock.
    return MockBroker(starting_balance=100_000.0), account.id
