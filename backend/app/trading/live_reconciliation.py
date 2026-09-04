"""Runs `ReconciliationWorker` (blueprint §75) for every account that has
an ACTIVE `BrokerAccount` connected AND has actually placed an order
during this API process's lifetime.

This deliberately runs *inside the API process*, not the separate
`worker` process (see `app/workers/main.py`) — `ReconciliationWorker`
needs the same `OrderManager`/`PositionManager` instances a user's live
orders were placed through (`app/api/orders.py`'s `_STACKS`), and those
only exist in the API process's memory. A `worker` process has no way to
reach them; running this loop there would just never find anything to
reconcile.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.api.orders import all_stacks
from app.core.redis import heartbeat
from app.database.models.users import BrokerAccount, BrokerAccountStatus
from app.database.session import async_session_factory
from app.workers.reconciliation_worker import ReconciliationWorker

logger = logging.getLogger("trading.live_reconciliation")


async def reconcile_all_connected_accounts() -> int:
    """Runs one reconciliation pass for every user who both (a) has an
    ACTIVE broker account and (b) already has a live trading stack in
    this process. Returns how many accounts were checked."""
    async with async_session_factory() as db:
        accounts = (
            await db.execute(select(BrokerAccount).where(BrokerAccount.status == BrokerAccountStatus.ACTIVE))
        ).scalars().all()

    stacks = all_stacks()
    checked = 0
    for account in accounts:
        stack = stacks.get(account.user_id)
        if stack is None:
            continue  # this user hasn't placed an order yet this process lifetime
        worker = ReconciliationWorker(
            account_id=str(account.user_id),
            user_id=account.user_id,
            broker=stack.broker,
            order_manager=stack.order_manager,
            position_manager=stack.position_manager,
        )
        try:
            await worker.run_once()
        except Exception:
            logger.exception("Reconciliation pass failed for account %s", account.id)
        checked += 1
    return checked


async def run(interval_seconds: float = 60.0) -> None:
    logger.info("Live reconciliation loop starting, interval=%ss", interval_seconds)
    while True:
        try:
            await reconcile_all_connected_accounts()
        except Exception:
            logger.exception("Live reconciliation pass failed")
        await heartbeat("reconciliation")
        await asyncio.sleep(interval_seconds)
