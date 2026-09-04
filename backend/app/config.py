from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables.

    Never hardcode secrets here; every field is meant to be supplied via
    the environment (see .env.example).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://trading:trading@localhost:5432/trading"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "change-me"
    environment: str = "development"


settings = Settings()
