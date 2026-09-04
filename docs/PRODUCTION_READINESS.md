# Production Readiness

This is an honest scorecard against the blueprint's own two checklists —
§132 "Production Readiness Checklist" and §119 "Data Licensing" / broker
compliance — plus the operational steps (secrets, deployment) neither
checklist covers. Read this before connecting a real account or real
money to this system.

## §132 Production Readiness Checklist

| Item | Status | Notes |
|---|---|---|
| Historical data validated | ❌ Not applicable yet | No real historical data provider is connected. `app/market` normalizes/aggregates whatever it's given; nothing has validated real market data because none has been ingested. |
| Market-data timestamps validated | ❌ Not applicable yet | Same — no live feed to validate. |
| No look-ahead bias | ✅ | Proven by test, not just claimed: `tests/replay/test_engine.py` runs the SMC engine on a truncated candle list and asserts future structure events are invisible. Every engine (replay, backtest, paper, auto-trade) only ever receives `candles[:t+1]`. |
| SMC/ICT tests passing | ✅ | `tests/smc`, `tests/strategy` |
| Replay tests passing | ✅ | `tests/replay` |
| Backtest tests passing | ✅ | `tests/backtest`, including out-of-sample validation (`validate_out_of_sample`) |
| Slippage model implemented | ✅ | `app.backtest.cost_model.CostModel`; `MockBroker` also simulates slippage/partial fills/rejections |
| Options execution tested | ⚠️ Partial | Greeks/payoff math is tested (`tests/options`); there is no options *order* execution path — no broker connects real options orders yet |
| Risk engine tested | ✅ | `tests/risk` — position sizing, all limit checks, kill switches |
| Broker authentication tested | ⚠️ Partial | Upstox OAuth flow implemented and tested end-to-end (`tests/api/test_brokers_upstox_oauth.py`) with a mocked token endpoint — **never tested against Upstox's real servers** (no credentials, and this sandbox can't reach upstox.com). Dhan has no implementation at all yet. |
| Order reconciliation tested | ⚠️ Partial | `ReconciliationWorker` + `app.trading.reconciliation` are tested (`tests/trading/test_reconciliation.py`, `tests/workers/test_reconciliation_worker.py`) — only against `MockBroker`, never a real broker's actual drift patterns |
| Duplicate-order protection tested | ✅ | Idempotency keys, `tests/trading/test_execution.py`; `POST /orders` now persists every order/fill to Postgres (`orders`/`order_events`/`positions`/`trades`, see `app/trading/persistence.py`) instead of only holding state in one API process's memory, and `tests/api/test_orders.py` proves a closing fill writes exactly one `Trade` journal row with the correct realized P&L |
| Broker-disconnect handling tested | ⚠️ Partial | `broker_healthy` check + reconciliation-triggered halt exist and are tested against `MockBroker`; no real broker to actually disconnect from |
| Market-data failure handling tested | ⚠️ Partial | Staleness check (`get_price_age_seconds`) is real and tested (`tests/test_core_redis.py`) against Redis; there's no live feed to actually go stale yet |
| Kill switch tested | ✅ | Strategy/account/global — `tests/risk/test_engine.py` |
| Audit logs working | ✅ | `audit_logs` + `risk_events` tables, wired into auth, orders, auto-trading, reconciliation |
| Paper trading successful | ✅ | `app/paper`, exercised end-to-end including inside the autonomous loop (`tests/workers/test_auto_trade_worker.py`) |
| Out-of-sample testing completed | ⚠️ Mechanism exists, not "completed" | `POST /backtest/validate` runs train/validation/test splits — but this is a tool, not a result; no actual strategy has been through it against real market data |
| Live trading limits configured | ❌ | No live account exists to configure limits *for* |
| User explicitly enabled auto trading | ✅ (mechanism) | `POST /auto-trading/enable` requires `confirm: true` + the `AUTO_TRADE` permission, and `POST /orders` (manual live trading) now requires the `LIVE_TRADE` permission the same way — both are opted into via `POST /trading-permissions/grant` (`confirm: true`), never granted by default (`tests/api/test_trading_permissions.py`). It still only ever drives `MockBroker`, so "enabling" either today enables **paper** trading, not real trading |

**Bottom line:** every item that can be verified without a live broker and
live market data has been — with a real, running test, not a claim. The
remaining ❌/⚠️ items are gated on infrastructure this environment
cannot provide: a live broker connection and a licensed market data feed.

## §119 Data Licensing & Compliance — not a code checklist

These are business/legal steps, not something any amount of code changes
here can satisfy:

- [ ] Market-data redistribution rights confirmed with your data provider
- [ ] Historical-data licensing confirmed
- [ ] Broker API terms of service reviewed (Dhan, Upstox)
- [ ] Exchange requirements for algorithmic trading reviewed
- [ ] **SEBI algorithmic trading compliance** (India-specific, and the one
      most likely to block autonomous trading specifically): exchange
      approval is required for an algo strategy before it can trade
      client funds autonomously, and brokers must register/tag
      API-driven orders as algo orders. This is not optional and this
      codebase does not — cannot — implement regulatory approval for you.

Do not flip a real account into `auto_trading_enabled=true` until these
are resolved.

## Secrets management

Every secret the app reads comes from environment variables (see
`.env.example`) — none are hardcoded, and `.gitignore` excludes `.env`.
For real deployment:

- **Generate real values**, don't ship the repo's dev defaults:
  - `JWT_SECRET`: any high-entropy random string.
  - `CREDENTIALS_ENCRYPTION_KEY`: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    This key encrypts broker credentials at rest (`broker_accounts.encrypted_credentials`)
    — losing it means losing access to every connected broker account;
    rotating it means re-encrypting or re-connecting every account.
- **Don't put secrets in `docker-compose.yml`'s `environment:` block** in
  production — use `env_file` (as it already does for app-level config)
  backed by your platform's secret store (e.g. Docker secrets, a cloud
  provider's secrets manager, Kubernetes Secrets), not committed files.
- **Rate limiting** (`RATE_LIMIT_ENABLED`) must be `true` in any
  environment reachable from the internet — it's only ever `false` in
  the test suite (see `tests/conftest.py`), where every request shares
  one client "IP".
- **CORS** (`CORS_ORIGINS`) defaults to empty (deny-all). Set it to your
  actual frontend origin(s) — never `*` alongside credentialed requests
  (this app doesn't use cookie auth, so that combination shouldn't come
  up, but don't introduce it).

## Deployment steps

1. Provision Postgres 16 and Redis 7 (or use `docker-compose.yml` as a
   starting point — it is not a production-grade Postgres/Redis setup:
   no backups, no replication, no TLS).
2. Copy `.env.example` to `.env`, fill in real secrets (see above) and
   real broker credentials once you have them.
3. Run migrations: `alembic upgrade head` (or the `migrate` service in
   `docker-compose.yml`).
4. Start the API (`uvicorn app.main:app`) and the worker
   (`python -m app.workers.main`) as separate, independently-scalable
   processes — see docs/ARCHITECTURE.md's "Cross-process design" for why
   they can't just share in-memory state.
5. Point `WORKER_SYMBOLS`/`WORKER_INSTRUMENT_IDS` at real instruments
   once a real market data feed exists (`app/workers/main.py` uses
   `SimulatedFeed` with no data source configured today — replace it with
   a real feed before expecting the worker to do anything).
6. Set up CI (`.github/workflows/ci.yml` already runs the full suite
   against Postgres/Redis service containers on every push/PR) and treat
   a red run as a hard merge blocker.
7. Add a reverse proxy in front of the API for TLS termination — see
   `infrastructure/nginx/nginx.conf.example` (not wired into
   docker-compose.yml — add it once you're running more than one API
   replica).
8. **Do not set `auto_trading_enabled=true` on any real account** until
   the §119 compliance items above are resolved and you've watched that
   account's strategy behave correctly in paper mode for a meaningful
   period.
