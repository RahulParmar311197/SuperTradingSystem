import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.dependencies import get_current_user
from app.auth.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SessionResponse,
    TokenResponse,
    UserResponse,
)
from app.core.rate_limit import rate_limit
from app.database.models.users import User, UserSession
from app.database.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

_login_rate_limit = rate_limit(limit=10, window_seconds=60, key_prefix="auth:login")
_register_rate_limit = rate_limit(limit=5, window_seconds=60, key_prefix="auth:register")


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_register_rate_limit)],
)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> User:
    try:
        return await auth_service.register(db, payload.email, payload.password, payload.name)
    except auth_service.AuthError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(_login_rate_limit)])
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    try:
        access_token, refresh_token = await auth_service.login(db, payload.email, payload.password)
    except auth_service.AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    try:
        access_token, refresh_token = await auth_service.refresh(db, payload.refresh_token)
    except auth_service.AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> None:
    """No auth dependency, same as `/refresh` -- a user whose access token
    has already expired but whose refresh token is still live must still
    be able to log out."""
    await auth_service.logout(db, payload.refresh_token)


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[UserSession]:
    return await auth_service.list_sessions(db, user.id)


@router.post("/sessions/{session_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> None:
    found = await auth_service.revoke_session(db, user.id, session_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
