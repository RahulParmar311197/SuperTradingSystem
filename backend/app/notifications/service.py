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

from app.database.models.notifications import Notification, NotificationType

logger = logging.getLogger("notifications")


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
    notification = Notification(user_id=user_id, type=notification_type, title=title, body=body, data=data or {})
    db.add(notification)
    await db.commit()
    await db.refresh(notification)
    await dispatch(notification)
    return notification
