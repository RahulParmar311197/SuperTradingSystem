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

Blueprint §74 "Broker Failure Handling" also covers the case a plain state
*mismatch* doesn't: the broker being unreachable at all (expired/revoked
token, network failure, an outage). `get_orders`/`get_positions` on a real
adapter raise rather than return an empty/stale result in that case (see
`UpstoxBroker._get`'s `response.raise_for_status()`, and the same
`(BrokerError, httpx.HTTPError, NotImplementedError)` surface every
adapter's own `is_healthy()` already treats as "not healthy") — that
failure is caught below the same way a mismatch is: halt, audit, notify.
"""

from __future__ import annotations

import logging

import httpx

from app.brokers.base import Broker, BrokerError
from app.core.audit import record_audit
from app.core.redis import account_halt_reason, halt_account
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
        try:
            broker_orders = await self.broker.get_orders()
            broker_positions = await self.broker.get_positions()
        except (BrokerError, httpx.HTTPError, NotImplementedError) as exc:
            reason = f"Broker unreachable during reconciliation: {exc}"
            logger.warning("Reconciliation could not reach the broker for account %s: %s", self.account_id, exc)
            # This worker runs on a 60-second loop for as long as an
            # account stays ACTIVE and connected -- resuming is a
            # deliberate manual admin action (see this module's own
            # docstring), so an outage that isn't noticed immediately
            # keeps this same `except` branch firing every single pass.
            # Without this check, every pass wrote a fresh AuditLog row
            # and Notification for the exact same, still-unresolved
            # incident -- flooding GET /notifications and the audit trail
            # with duplicates for one outage instead of recording it once.
            already_halted = await account_halt_reason(self.account_id) is not None
            await halt_account(self.account_id, reason)

            if not already_halted:
                async with async_session_factory() as db:
                    await record_audit(
                        db,
                        actor="system",
                        action="reconciliation.broker_unreachable",
                        user_id=self.user_id,
                        details={"error": str(exc)},
                    )
                    await create_notification(
                        db,
                        user_id=self.user_id,
                        notification_type=NotificationType.BROKER_DISCONNECTED,
                        title="Broker disconnected",
                        body="Could not reach the broker during reconciliation — new entries are halted until this is resolved.",
                        data={"error": str(exc)},
                    )
            return ReconciliationReport(order_mismatches=[reason])

        local_orders = self.order_manager.list_all(self.account_id)
        local_positions = self.position_manager.open_positions(self.account_id)

        report = reconcile(local_orders, broker_orders, local_positions, broker_positions)

        if not report.in_sync:
            reason = "; ".join(report.order_mismatches + report.position_mismatches)
            logger.warning("Reconciliation mismatch for account %s: %s", self.account_id, reason)
            # Same repeat-fire problem as the broker-unreachable branch
            # above -- an unresolved mismatch stays `not in_sync` on every
            # subsequent pass until an admin resolves and resumes it, so
            # this must only alert once per incident, not once per minute.
            already_halted = await account_halt_reason(self.account_id) is not None
            await halt_account(self.account_id, f"Reconciliation mismatch: {reason}")

            if not already_halted:
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
