from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration, loaded from environment variables.

    See .env.example at the repository root for the full list of variables.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AI Trading Platform"
    environment: str = "development"
    debug: bool = True

    database_url: str = "postgresql+asyncpg://trading:trading@localhost:5432/trading"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "change-me-in-production"
    # Fernet key (44-char urlsafe-base64). Generate with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
    # and set via env var in production — this default is dev-only.
    credentials_encryption_key: str = "_dkl40L0HRwEUATk-3h1L3pIGhQjrf6pH1sMBD2SqY4="
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    ai_api_key: str | None = None
    ai_provider: str = "none"
    ai_model: str = "claude-sonnet-4-5"

    dhan_client_id: str | None = None
    dhan_secret: str | None = None

    upstox_client_id: str | None = None
    upstox_secret: str | None = None

    # Empty by default — deny cross-origin browser requests until an
    # operator explicitly lists allowed origins. JWTs travel in the
    # Authorization header (not cookies), so credentialed CORS isn't
    # needed even once origins are configured.
    cors_origins: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
