import pytest

from app.core.config import Settings

_REAL_JWT_SECRET = "a-real-high-entropy-secret-generated-for-this-deployment"
_REAL_ENCRYPTION_KEY = "P8H2q3vN7kZmR5tYcW1xU9jL4bE0aFgD6sV3nQoT8yM="


def _settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql+asyncpg://x:x@localhost:5432/x",
        "redis_url": "redis://localhost:6379/0",
        "jwt_secret": _REAL_JWT_SECRET,
        "credentials_encryption_key": _REAL_ENCRYPTION_KEY,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_development_settings_accept_the_repository_defaults():
    # The whole point of these defaults is that local dev/tests work with
    # zero configuration -- this must never start raising just because
    # `environment` stays at its own default ("development"), regardless
    # of whether jwt_secret/credentials_encryption_key are still the
    # class-level defaults or overridden by a local .env (as this sandbox's
    # own .env does for jwt_secret).
    Settings(database_url="x", redis_url="x", credentials_encryption_key="_dkl40L0HRwEUATk-3h1L3pIGhQjrf6pH1sMBD2SqY4=")


def test_production_with_default_jwt_secret_refuses_to_start():
    # Regression test: `credentials_encryption_key`'s default isn't an
    # obviously-invalid placeholder like jwt_secret's -- it's a real,
    # working Fernet key committed to source, encrypting every connected
    # broker account's OAuth credentials at rest. Nothing previously
    # checked that a real deployment actually overrode it (or jwt_secret)
    # before serving traffic; `environment` itself was declared but read
    # nowhere in the codebase.
    with pytest.raises(ValueError, match="JWT_SECRET"):
        _settings(environment="production", jwt_secret="change-me-in-production")


def test_production_with_default_encryption_key_refuses_to_start():
    with pytest.raises(ValueError, match="CREDENTIALS_ENCRYPTION_KEY"):
        _settings(environment="production", credentials_encryption_key="_dkl40L0HRwEUATk-3h1L3pIGhQjrf6pH1sMBD2SqY4=")


def test_production_with_blank_encryption_key_refuses_to_start():
    # .env.example ships CREDENTIALS_ENCRYPTION_KEY= blank by design (to
    # force an operator to notice and fill it in) -- an operator who
    # copies it and simply forgets gets an empty string, not the class
    # default, so the check above alone wouldn't have caught this.
    with pytest.raises(ValueError, match="CREDENTIALS_ENCRYPTION_KEY"):
        _settings(environment="production", credentials_encryption_key="")


def test_production_with_real_secrets_starts_normally():
    settings = _settings(environment="production")
    assert settings.jwt_secret == _REAL_JWT_SECRET
    assert settings.credentials_encryption_key == _REAL_ENCRYPTION_KEY
