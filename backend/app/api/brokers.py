import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.brokers.upstox import build_authorization_url, exchange_code_for_token
from app.core.config import get_settings
from app.core.encryption import encrypt_credentials
from app.core.redis import pop_oauth_state, store_oauth_state
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


@router.get("/upstox/authorize")
async def upstox_authorize(user: User = Depends(get_current_user)) -> dict:
    """Step 1 of the Upstox OAuth flow (blueprint §52, §69): returns the
    URL the client should send the user's browser to. The `state` value
    ties the eventual callback back to this user without requiring the
    callback request itself to carry a bearer token (it won't — it's a
    browser redirect from Upstox)."""
    settings = get_settings()
    if not settings.upstox_client_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "UPSTOX_CLIENT_ID is not configured")

    state = str(uuid.uuid4())
    await store_oauth_state(state, str(user.id))
    url = build_authorization_url(settings.upstox_client_id, settings.upstox_redirect_uri, state=state)
    return {"authorization_url": url}


@router.get("/upstox/callback", response_model=BrokerAccountResponse)
async def upstox_callback(code: str, state: str, db: AsyncSession = Depends(get_db)) -> BrokerAccount:
    """Step 2: Upstox redirects the user's browser here with `code`. No
    auth header is available on this request — `state` (stored at
    /authorize time) is what tells us which user this belongs to."""
    settings = get_settings()
    if not settings.upstox_client_id or not settings.upstox_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Upstox credentials are not configured")

    user_id = await pop_oauth_state(state)
    if user_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OAuth state is invalid or has expired — restart the connection")

    try:
        token_response = await exchange_code_for_token(
            settings.upstox_client_id, settings.upstox_secret, settings.upstox_redirect_uri, code
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Upstox token exchange failed: {exc}") from exc

    if "access_token" not in token_response:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Upstox token response did not include an access_token")

    account = BrokerAccount(
        user_id=uuid.UUID(user_id),
        broker=BrokerName.UPSTOX,
        encrypted_credentials=encrypt_credentials(token_response),
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
