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
| Replay tests passing | ✅ | `tests/replay` (pure engine unit tests) plus `tests/api/test_replay_persistence.py` (the REST layer: `replay_sessions`/`replay_orders` now actually persist to Postgres, blueprint §9, and a session's owner is enforced — a second user gets 404, not another user's state). The API layer had zero integration tests before this and every `/replay/*` endpoint was crashing (see "The `.__dict__` bug" below) — `tests/replay` alone never would have caught it, since it never goes through `app/api/replay.py`. |
| Backtest tests passing | ✅ | `tests/backtest`, including out-of-sample validation (`validate_out_of_sample`), plus `tests/api/test_backtest_run.py` for `POST /backtest` itself — which, like replay, had never been exercised by a test and was crashing on every call (see "The `.__dict__` bug" below) |
| Slippage model implemented | ✅ | `app.backtest.cost_model.CostModel`; `MockBroker` also simulates slippage/partial fills/rejections |
| Options execution tested | ⚠️ Partial | Greeks/payoff math is tested (`tests/options`). Both single-leg (`POST /orders`) and **multi-leg** (`POST /options/execute`, blueprint §37-40) execution are real and tested: the latter gates the whole combination's payoff (`app/risk/options_risk.py`) through one risk check — not a per-leg entry/stop, which doesn't apply to a defined-risk combination — checks each leg's liquidity via `OptionSnapshot` when it exists, and submits every leg through the same broker/persistence pipeline `POST /orders` uses (`tests/api/test_options_execute.py` proves a real 2-leg spread persists both `Order`/`Position` rows with the correct net premium and max loss). What's still honestly missing: no options-chain *ingestion* pipeline exists to populate `OptionContract`/`OptionSnapshot` in the first place (see Stage 1's note on `app/market`'s missing live feed), so the liquidity check only ever warns today, never actually rejects on real data; and multi-leg execution is **not atomic** — each leg is a separate broker order, and neither this codebase nor (unverified) Upstox/Dhan guarantee all-or-nothing fills, so a partial-leg failure is reported per-leg rather than rolled back. |
| Risk engine tested | ✅ | `tests/risk` — position sizing, all limit checks, kill switches, plus the correlation engine (blueprint §85-86: `app/risk/correlation.py` pure math, `app/risk/portfolio.py` real-candle-history integration) and the `correlated_exposure_limit` check it feeds |
| Broker authentication tested | ⚠️ Partial | Upstox OAuth flow implemented and tested end-to-end (`tests/api/test_brokers_upstox_oauth.py`) with a mocked token endpoint — **never tested against Upstox's real servers** (no credentials, and this sandbox can't reach upstox.com). Dhan has no implementation at all yet. `app/trading/broker_resolver.py` (tested: `tests/trading/test_broker_resolver.py`) now actually selects the right adapter for a connected account instead of always `MockBroker` — the untested part is specifically Upstox's/Dhan's own HTTP calls against real servers, not the selection logic. |
| Order reconciliation tested | ⚠️ Partial | `ReconciliationWorker` + `app.trading.reconciliation` are tested (`tests/trading/test_reconciliation.py`, `tests/workers/test_reconciliation_worker.py`) — only against `MockBroker`, never a real broker's actual drift patterns. It's now actually wired up and running (`app/trading/live_reconciliation.py`, tested end-to-end in `tests/trading/test_live_reconciliation.py`: a broker-side position with no local match really does halt the account), which it previously wasn't — "isn't started here" was a comment in `app/workers/main.py`, not a real gap in the reconciliation logic itself. The other half of §75 — resuming is a *deliberate manual step* — is now real too: `GET /admin/halted-accounts` / `POST /admin/accounts/{id}/resume` (`tests/api/test_admin.py`), where before `app.core.redis.resume_account` existed but nothing in the API ever called it, meaning a halt had no way to be lifted short of editing Redis directly. |
| Duplicate-order protection tested | ✅ | Idempotency keys, `tests/trading/test_execution.py`; `POST /orders` now persists every order/fill to Postgres (`orders`/`order_events`/`positions`/`trades`, see `app/trading/persistence.py`) instead of only holding state in one API process's memory, and `tests/api/test_orders.py` proves a closing fill writes exactly one `Trade` journal row with the correct realized P&L |
| Broker-disconnect handling tested | ⚠️ Partial | `broker_healthy` check + reconciliation-triggered halt exist and are tested against `MockBroker` and now actually run continuously for connected accounts (see Order reconciliation above); no real broker to actually disconnect from |
| Market-data failure handling tested | ⚠️ Partial | Staleness check (`get_price_age_seconds`) is real and tested (`tests/test_core_redis.py`) against Redis; there's no live feed to actually go stale yet |
| Kill switch tested | ✅ | Strategy/account/global — `tests/risk/test_engine.py` |
| Audit logs working | ✅ | `audit_logs` + `risk_events` tables, wired into auth, orders, auto-trading, reconciliation; `ai_decisions` now has a real writer too (`POST /ai/propose-trade`) where before the table existed with zero rows ever written to it; visible cross-account via `GET /admin/*` (blueprint §116) for the `ADMIN` role, which never exposes `encrypted_credentials` |
| Paper trading successful | ✅ | `app/paper`, exercised end-to-end including inside the autonomous loop (`tests/workers/test_auto_trade_worker.py`). The manual `/paper/*` REST API (`app/api/paper.py`) is now also tested (`tests/api/test_paper.py`) — the engine was always real, but the router had zero tests and, until this round, no ownership check at all: any authenticated user who knew another user's session UUID could read its state and feed candles into it |
| Out-of-sample testing completed | ⚠️ Mechanism exists, not "completed" | `POST /backtest/validate` runs train/validation/test splits — but this is a tool, not a result; no actual strategy has been through it against real market data |
| Live trading limits configured | ❌ | No live account exists to configure limits *for* |
| User explicitly enabled auto trading | ✅ (mechanism) | `POST /auto-trading/enable` requires `confirm: true` + the `AUTO_TRADE` permission, and `POST /orders` (manual live trading) now requires the `LIVE_TRADE` permission the same way — both are opted into via `POST /trading-permissions/grant` (`confirm: true`), never granted by default (`tests/api/test_trading_permissions.py`). It still only ever drives `MockBroker`, so "enabling" either today enables **paper** trading, not real trading — and the persisted `orders`/`positions`/`trades` rows now actually say `PAPER`, too (`tests/api/test_orders.py`, `tests/api/test_options_execute.py`): every manual order placed through `POST /orders`/`POST /options/execute` was being journaled as `LIVE` regardless of which broker actually filled it, a real blueprint §101 violation only caught once a test finally asserted on `execution_mode` instead of just the order's status |

**The `.__dict__` bug:** five response-building call sites across this
codebase (`app/api/replay.py`, `app/api/ai.py`, `app/api/backtest.py`,
`app/api/markets.py`, and this change's own new
`app/replay/persistence.py`) called `.__dict__` on a `@dataclass(slots=True)`
instance — which has no `__dict__` and raises `AttributeError`
unconditionally. `POST /replay`, every other `/replay/*` endpoint,
`POST /ai/explain-trade`, `POST /backtest` (the run endpoint), and
`GET /candles` were all a guaranteed 500 on every call. Four of those five
endpoints had never once been hit by an integration test before this
round, which is exactly how a 100%-broken code path survived every
previous test run. All five are fixed (`dataclasses.asdict(...)`) and now
have a passing regression test that actually calls the endpoint —
`tests/api/test_replay_persistence.py`,
`tests/api/test_ai_propose_trade.py::test_explain_trade_returns_explanation_without_crashing`,
`tests/api/test_backtest_run.py`, `tests/api/test_markets_candles.py`.
Worth stating plainly: this is not a reason to fully trust every other
endpoint either — it's a reason to keep writing integration tests for any
endpoint that doesn't have one yet, since "the code looks right" and "a
real request against it works" are not the same claim.

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

- **Set `ENVIRONMENT=production`.** It's `development` by default, which
  is what lets local dev and the test suite run with zero configuration.
  Setting it to `production` makes the app refuse to start
  (`app/core/config.py`'s `Settings._refuse_default_secrets_in_production`)
  if `JWT_SECRET` or `CREDENTIALS_ENCRYPTION_KEY` is still blank or a
  repo default — this is the enforcement mechanism for the next bullet,
  not just documentation of intent.
- **Generate real values**, don't ship the repo's dev defaults:
  - `JWT_SECRET`: any high-entropy random string.
  - `CREDENTIALS_ENCRYPTION_KEY`: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    This key encrypts broker credentials at rest (`broker_accounts.encrypted_credentials`)
    — losing it means losing access to every connected broker account;
    rotating it means re-encrypting or re-connecting every account. Its
    repo default is a real, working key, committed to source — treat it
    as already public, never as a fallback for a forgotten override.
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
   replica). **Run exactly one API replica** if any account has a
   connected broker: the order/position state and the live reconciliation
   loop that protects it (`app/trading/broker_resolver.py`,
   `app/trading/live_reconciliation.py`) both live in that one process's
   memory — see docs/ARCHITECTURE.md's "Multiple API replicas for the
   manual /orders path".
8. **Do not set `auto_trading_enabled=true` on any real account** until
   the §119 compliance items above are resolved and you've watched that
   account's strategy behave correctly in paper mode for a meaningful
   period.
