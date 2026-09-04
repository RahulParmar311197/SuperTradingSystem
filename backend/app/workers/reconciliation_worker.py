"""ReconciliationWorker (blueprint §75): periodically compares local
order/position state against the broker's and halts new entries for an
account the moment they disagree — see blueprint §74's "never assume the
local position state is correct after a disconnect."

Halting is a Redis flag (`app.core.redis.halt_account`), not an in-memory
one, because this worker runs in its own process, separate from the API
process(es) that actually place orders (see docker-compose.yml's `worker`
service) — an in-memory flag here would never be seen there.

Resuming is deliberately NOT automatic: a mismatch means something needs a
human look, so `resume_account` must be called explicitly (e.g. from an
admin action) once the discrepancy is understood and resolved.
"""

from __future__ import annotations

import logging

from app.brokers.base import Broker
from app.core.audit import record_audit
from app.core.redis import halt_account
from app.database.models.notifications import NotificationType
from app.database.session import async_session_factory
from app.notifications.service import create_notification
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager
from app.trading.reconciliation import ReconciliationReport, reconcile

logger = logging.getLogger("workers.reconciliation")


class ReconciliationWorker:
    def __init__(
        self,
        account_id: str,
        user_id,  # uuid.UUID — kept loose to avoid importing uuid just for a type hint here
        broker: Broker,
        order_manager: OrderManager,
        position_manager: PositionManager,
    ) -> None:
        self.account_id = account_id
        self.user_id = user_id
        self.broker = broker
        self.order_manager = order_manager
        self.position_manager = position_manager

    async def run_once(self) -> ReconciliationReport:
        broker_orders = await self.broker.get_orders()
        broker_positions = await self.broker.get_positions()
        local_orders = self.order_manager.list_all(self.account_id)
        local_positions = self.position_manager.open_positions(self.account_id)

        report = reconcile(local_orders, broker_orders, local_positions, broker_positions)

        if not report.in_sync:
            reason = "; ".join(report.order_mismatches + report.position_mismatches)
            logger.warning("Reconciliation mismatch for account %s: %s", self.account_id, reason)
            await halt_account(self.account_id, f"Reconciliation mismatch: {reason}")

            async with async_session_factory() as db:
                await record_audit(
                    db,
                    actor="system",
                    action="reconciliation.mismatch",
                    user_id=self.user_id,
                    details={"order_mismatches": report.order_mismatches, "position_mismatches": report.position_mismatches},
                )
                await create_notification(
                    db,
                    user_id=self.user_id,
                    notification_type=NotificationType.RECONCILIATION_REQUIRED,
                    title="Reconciliation required",
                    body="Local and broker state disagree — new entries are halted until this is resolved.",
                    data={"order_mismatches": report.order_mismatches, "position_mismatches": report.position_mismatches},
                )

        return report
