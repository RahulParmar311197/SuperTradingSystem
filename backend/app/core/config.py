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

    # Market data only, and deliberately separate from the per-user
    # `BrokerAccount` credentials above. `resolve_broker` routes every
    # order to the most recent ACTIVE BrokerAccount, so a token stored
    # there to fetch prices would also send that user's orders to Upstox;
    # this one is process-wide, creates no account row, and is consumed
    # only by `app.market.providers.upstox.UpstoxMarketData`, which has no
    # method that could place an order. Unset (the default) means no live
    # feed -- the workers fall back to the simulated one.
    #
    # Upstox issues no read-only market-data token: this value CAN trade
    # through any other client. Treat it as a trading credential wherever
    # it is stored, and note Upstox expires it daily (~03:30 IST).
    upstox_data_access_token: str | None = None

    # Empty by default — deny cross-origin browser requests until an
    # operator explicitly lists allowed origins. JWTs travel in the
    # Authorization header (not cookies), so credentialed CORS isn't
    # needed even once origins are configured.
    cors_origins: list[str] = []

    # Off in tests/CI by default (see tests/conftest.py) — every request
    # from a test suite shares one client "IP", so a real limit would trip
    # on nothing but test volume rather than actual abuse.
    rate_limit_enabled: bool = True

    # What to do when the limiter cannot reach Redis. This used to be an
    # accident rather than a choice: `check_rate_limit` is a bare
    # `redis.incr`/`expire`, the dependency did not catch it, and a Redis
    # outage therefore turned `POST /auth/login` and `POST /auth/register`
    # into unhandled 500s -- measured. Both the deny and the allow reading
    # are defensible and neither is free:
    #
    #   False (default, and what the 500 effectively did): deny. Nobody
    #     logs in while Redis is down, including the operator who needs
    #     the admin endpoints to resume halted accounts and lift the kill
    #     switch -- both of which live in Redis (see
    #     app/risk/kill_switch.py).
    #   True: allow. Login stays up, and brute-force protection is gone
    #     for the duration, which is also exactly when an attacker who
    #     caused the outage would want it gone.
    #
    # DECIDED: deny. This was left as an open question when the 500 was
    # fixed; it is now settled on merit rather than on caution.
    #
    # The limiter's job on `/auth/login` is to blunt credential stuffing.
    # Failing open during a Redis outage hands an attacker exactly the
    # window they would engineer if they could -- take out the shared
    # cache, then brute-force unthrottled -- which makes the outage an
    # amplifier rather than a nuisance.
    #
    # The operator-lockout argument is real but weaker than it first
    # looks: with Redis down, `account_halt_reason` and the kill switch
    # cannot be read either (app/core/redis.py), so the admin actions
    # someone would log in to perform would not work regardless. The
    # remedy for "Redis is down" is to restore Redis, which
    # docker-compose.yml's `restart: unless-stopped` now does on its own.
    #
    # Set it True only for a deployment where login availability genuinely
    # outranks brute-force protection, and know that is the trade.
    rate_limit_fail_open: bool = False

    # How many trusted reverse proxies sit in front of this process.
    # 0 (the default) means "none": the rate limiter keys on the socket
    # peer, which is then the real client. Behind a proxy the peer is the
    # *proxy*, so every user in the world shares one bucket -- measured
    # with the repo's own `infrastructure/nginx/nginx.conf.example`:
    # twelve distinct client IPs, and the eleventh and twelfth got 429.
    # Set this to the number of proxies you actually run (1 for that nginx
    # example) and the limiter reads that many entries back from the right
    # of `X-Forwarded-For`, which is the address the innermost trusted
    # proxy observed. It is deliberately opt-in rather than automatic:
    # trusting the header with no proxy in front lets any client forge its
    # own address, evading the limiter and locking other users out of
    # login by forging theirs.
    trusted_proxy_hops: int = 0

    @model_validator(mode="after")
    def _refuse_unsafe_defaults_in_production(self) -> "Settings":
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

        `debug` belongs to the same family and was missed: it defaults to
        `True`, and two readers act on it --
        `app/main.py`'s unhandled-exception handler returns `str(exc)` to
        the client when it is set, and `create_async_engine` passes it as
        `echo`. A deployment that correctly overrode both secrets but
        never set `DEBUG=false` therefore answers every 500 with raw
        exception text (including the failing SQL) and logs every
        statement it runs. Same failure mode as the two above -- an
        operator not overriding a development default -- so it is refused
        the same way rather than fixed up silently: an operator who set
        `DEBUG=true` deliberately should find out at startup, not discover
        later that the value was ignored.
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
            if self.debug:
                raise ValueError(
                    "DEBUG is enabled (its default) while ENVIRONMENT=production. That returns raw exception "
                    "text -- including the failing SQL statement -- to unauthenticated clients on every 500, "
                    "and echoes every SQL statement to the logs. Set DEBUG=false before running with "
                    "ENVIRONMENT=production."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
