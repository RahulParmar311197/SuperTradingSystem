"""Audit logging (blueprint §71): every user/system/AI decision that
matters should be reconstructable later."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.risk import AuditLog


async def record_audit(
    db: AsyncSession, actor: str, action: str, user_id: uuid.UUID | None = None, details: dict | None = None
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            actor=actor,
            action=action,
            details=details or {},
            occurred_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
