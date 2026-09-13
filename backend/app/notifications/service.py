"""Notifications (blueprint §63, §104). Persists an in-app notification and
hands it to a pluggable dispatcher for push delivery — no push provider
(FCM/APNs) is wired up in this environment, so the default dispatcher just
logs, which keeps the call site (risk/execution/market-data code) identical
once a real one is plugged in.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import clip
from app.database.models.notifications import Notification, NotificationType

logger = logging.getLogger("notifications")

# Taken from the columns themselves rather than restated, so a widened
# column cannot leave a stale number here. The bug being fixed was two
# places disagreeing about one width.
_TITLE_LIMIT = Notification.__table__.c.title.type.length
_BODY_LIMIT = Notification.__table__.c.body.type.length


class NotificationDispatcher(ABC):
    @abstractmethod
    async def send(self, notification: Notification) -> None: ...


class LoggingDispatcher(NotificationDispatcher):
    async def send(self, notification: Notification) -> None:
        logger.info("notification user=%s type=%s title=%s", notification.user_id, notification.type.value, notification.title)


_dispatcher: NotificationDispatcher = LoggingDispatcher()


def set_dispatcher(dispatcher: NotificationDispatcher) -> None:
    global _dispatcher
    _dispatcher = dispatcher


async def dispatch(notification: Notification) -> None:
    await _dispatcher.send(notification)


async def create_notification(
    db: AsyncSession, user_id: uuid.UUID, notification_type: NotificationType, title: str, body: str, data: dict | None = None
) -> Notification:
    # `body` regularly carries text a broker wrote -- a rejection reason
    # quoted into "this position has no stop-loss", for one. Measured: a
    # 996-character stop rejection made this insert raise, so the account
    # holder was never told their live position was unprotected, and the
    # request 500ed after the halt had already been set. The message is
    # diagnostic; its tail is worth less than the alert. `data` is JSON
    # and unbounded, so nothing is lost from the machine-readable half.
    notification = Notification(
        user_id=user_id,
        type=notification_type,
        title=clip(title, _TITLE_LIMIT),
        body=clip(body, _BODY_LIMIT),
        data=data or {},
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)
    await dispatch(notification)
    return notification
