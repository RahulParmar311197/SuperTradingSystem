from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Both committed here as recognizable, dev-only placeholders -- see the
# Settings model_validator below, which refuses to start with either of
# them still in place once `environment` says "production".
_DEV_JWT_SECRET = "change-me-in-production"
_DEV_CREDENTIALS_ENCRYPTION_KEY = "_dkl40L0HRwEUATk-3h1L3pIGhQjrf6pH1sMBD2SqY4="


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

    jwt_secret: str = _DEV_JWT_SECRET
    # Fernet key (44-char urlsafe-base64). Generate with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
    # and set via env var in production — this default is dev-only.
    credentials_encryption_key: str = _DEV_CREDENTIALS_ENCRYPTION_KEY
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
    upstox_redirect_uri: str = "http://localhost:8000/brokers/upstox/callback"

    # Empty by default — deny cross-origin browser requests until an
    # operator explicitly lists allowed origins. JWTs travel in the
    # Authorization header (not cookies), so credentialed CORS isn't
    # needed even once origins are configured.
    cors_origins: list[str] = []

    # Off in tests/CI by default (see tests/conftest.py) — every request
    # from a test suite shares one client "IP", so a real limit would trip
    # on nothing but test volume rather than actual abuse.
    rate_limit_enabled: bool = True

    @model_validator(mode="after")
    def _refuse_default_secrets_in_production(self) -> "Settings":
        """`credentials_encryption_key`'s default isn't an obviously-invalid
        placeholder the way `jwt_secret`'s is — it's a real, working Fernet
        key, committed to this source tree, that encrypts every connected
        broker account's OAuth credentials at rest
        (`broker_accounts.encrypted_credentials`). Nothing before this
        validator ever checked that a real deployment actually overrode
        either default before serving traffic; `environment` itself was
        declared but never read anywhere in the codebase. Set
        `ENVIRONMENT=production` (see docs/PRODUCTION_READINESS.md) to
        turn this into a hard startup failure instead of a silent,
        publicly-known secret in production.
        """
        if self.environment == "production":
            # Blank counts as unset too -- .env.example ships
            # `CREDENTIALS_ENCRYPTION_KEY=` empty by design (forcing an
            # operator to notice and fill it in), but pydantic-settings
            # treats that as an explicit empty-string override of the
            # class default, not "use the default" -- so an operator who
            # copies .env.example and simply forgets to fill it in would
            # otherwise sail past a check that only compared against the
            # committed default string.
            if not self.jwt_secret or self.jwt_secret == _DEV_JWT_SECRET:
                raise ValueError(
                    "JWT_SECRET is empty or still the repository's dev-only default. Set a real, high-entropy "
                    "value via the JWT_SECRET environment variable before running with ENVIRONMENT=production."
                )
            if not self.credentials_encryption_key or self.credentials_encryption_key == _DEV_CREDENTIALS_ENCRYPTION_KEY:
                raise ValueError(
                    "CREDENTIALS_ENCRYPTION_KEY is empty or still the repository's dev-only default -- a real "
                    "Fernet key committed to source, so it is not a secret. Generate a real one "
                    '(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") '
                    "and set it via the CREDENTIALS_ENCRYPTION_KEY environment variable before running with "
                    "ENVIRONMENT=production."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
