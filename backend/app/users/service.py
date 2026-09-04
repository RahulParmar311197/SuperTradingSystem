import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.users import User, UserRole, UserStatus


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, email: str, password_hash: str, name: str) -> User:
    user = User(
        email=email,
        password_hash=password_hash,
        name=name,
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        trading_permissions=["VIEW", "ANALYZE", "PAPER_TRADE"],
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user
