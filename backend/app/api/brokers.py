import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.encryption import encrypt_credentials
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User
from app.database.session import get_db

router = APIRouter(prefix="/brokers", tags=["brokers"])


class BrokerAccountResponse(BaseModel):
    id: uuid.UUID
    broker: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConnectBrokerRequest(BaseModel):
    broker: BrokerName
    credentials: dict  # e.g. {"client_id": "...", "access_token": "..."} — never logged, only encrypted at rest


@router.get("", response_model=list[BrokerAccountResponse])
async def list_brokers(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[BrokerAccount]:
    stmt = select(BrokerAccount).where(BrokerAccount.user_id == user.id)
    return (await db.execute(stmt)).scalars().all()


@router.post("/connect", response_model=BrokerAccountResponse, status_code=status.HTTP_201_CREATED)
async def connect_broker(
    payload: ConnectBrokerRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BrokerAccount:
    # NOTE: this only stores credentials encrypted at rest. Before enabling
    # live trading, the broker adapter (see app/brokers/dhan, app/brokers/upstox)
    # must actually authenticate with them (blueprint §120 step 3).
    account = BrokerAccount(
        user_id=user.id,
        broker=payload.broker,
        encrypted_credentials=encrypt_credentials(payload.credentials),
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_broker(
    account_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> None:
    account = await db.get(BrokerAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Broker account not found")
    await db.delete(account)
    await db.commit()
