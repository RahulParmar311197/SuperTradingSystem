from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.dependencies import get_current_user
from app.auth.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserResponse
from app.core.rate_limit import rate_limit
from app.database.models.users import User
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


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
