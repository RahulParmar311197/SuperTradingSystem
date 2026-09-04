# Implementation Status

This tracks what exists in code against the stages in
`AI_TRADING_PLATFORM_BLUEPRINT.md` §134 ("Project Status Definition").
For the honest go/no-go on real money, see
[`PRODUCTION_READINESS.md`](PRODUCTION_READINESS.md) — this file is about
what's built, that file is about what's actually safe to turn on.

| Stage | Blueprint area | Status |
|---|---|---|
| 0 | Architecture | Backend scaffolded (`backend/app/*`), repo layout matches §129. Structured logging, request-id tracing, Prometheus `/metrics`, a startup health check (lifespan), audit logging (`audit_logs`, `risk_events`), and CI (`.github/workflows/ci.yml`, runs the full suite against Postgres/Redis on every push/PR) are wired in — see §72/§71. `GET /health` now reports real **worker liveness** (blueprint §117 "Workers 🟢"): each of the `market_data`, `scanner`, and `auto_trade` loops in `app/workers/main.py` refreshes a short-TTL Redis heartbeat every pass (`app.core.redis.heartbeat`), so a stuck or never-started worker process reads `DOWN` honestly instead of an assumed `HEALTHY`. An **admin dashboard** (`app/api/admin.py`, blueprint §115-116) exposes users, broker connections (never `encrypted_credentials`), orders, risk events, and this same worker/component health, gated on `UserRole.ADMIN`. |
| 1 | Market data | `app/market`: normalization, timeframe/candle aggregation, simulated feed. `app/workers/market_data_worker.py` + `candle_worker.py` consume a feed, update the Redis latest-price cache, persist closed candles, derive higher timeframes, and publish on `/ws/market` + `/ws/chart`. No *live* broker feed yet — `app/workers/main.py` runs these against `SimulatedFeed` with no data source configured, so the worker process is a real, tested pipeline waiting on a real feed. |
| 2 | SMC/ICT | `app/smc`, `app/ict`: swings, BOS/CHoCH/MSS, liquidity + sweeps, FVG, order blocks, premium/discount, kill zones, opening ranges. Fully unit-tested, look-ahead safe by construction. |
| 3 | Replay | `app/replay`: clock + manual BUY/SELL/SL/TP/CLOSE, statistics. Look-ahead safety proven by test (`tests/replay/test_engine.py`). Exposed over REST (`app/api/replay.py`) with a per-session in-memory store. |
| 4 | Backtesting | `app/backtest`: event loop reusing the same SMC/ICT/Strategy code as replay, configurable cost model, full metrics report. **Out-of-sample validation** (`POST /backtest/validate`, blueprint §77-78) runs train/validation/test splits independently and flags overfitting smells (no trades or no edge on the held-out test period, a win-rate collapse from train to validation). Persists `backtests`/`backtest_trades`/`backtest_metrics` via `app/api/backtest.py`. |
| 5 | AI | `app/ai`: structured context builder, Strategy-DSL JSON validation, AI trade-proposal validation against deterministic results, deterministic trade explanations. **A real provider is wired**: `app/ai/providers/anthropic_client.py` implements `AIClient` against the Claude API — set `AI_PROVIDER=anthropic` + `AI_API_KEY` to enable it. With no key configured, `NullAIClient` fails closed (§110 "no AI -> no trade"). |
| 6 | Options | `app/options`: Black-Scholes Greeks, multi-leg payoff engine (max profit/loss/breakevens), liquidity filter, named strategy builders (spreads, condor, butterfly, straddle, strangle). **Execution exists now too**: `POST /options/execute` (blueprint §37-40) submits every leg of a chosen strategy as real orders — see "Multi-leg options execution" below. |
| 7 | Paper trading | `app/paper`: strategy -> risk -> broker -> position manager -> portfolio, built on the same order/broker stack as live trading. Now also runs unattended inside the autonomous loop (Stage 10). |
| 8 | Dhan/Upstox | `app/brokers/dhan`: adapter skeleton, every HTTP call still a `NotImplementedError` TODO. **`app/brokers/upstox` is a real implementation** — OAuth2 authorization-code flow (`app/brokers/upstox/oauth.py`, plus `GET /brokers/upstox/authorize` and `/callback`), and `UpstoxBroker` implements every `Broker` method against Upstox's documented v2 API. Built from search-result snippets, not a fetched/verified copy of the live docs (this sandbox's egress to upstox.com is blocked) — tested against a mocked HTTP transport (`tests/brokers/test_upstox_adapter.py`), **never against Upstox's real servers**. Verify every endpoint/field/status-string against the live docs or Postman collection before connecting a real account. |
| 9 | Controlled live trading | `app/api/orders.py` exercises the full risk-gate -> execution -> position flow. **The broker is no longer hardcoded**: `app/trading/broker_resolver.py` looks up the user's connected `BrokerAccount` and returns a real `UpstoxBroker`/`DhanBroker` built from their stored (decrypted) credentials for an ACTIVE connection, or `MockBroker` when nothing is connected — the honest Stage 9 default, not a workaround. A broken connection (missing/malformed stored credentials) raises rather than silently falling back to Mock (blueprint §101: "Never make paper and live look identical"). Gated behind the `LIVE_TRADE` trading permission (blueprint §88), which — like `AUTO_TRADE` — isn't granted at registration; a user opts in via `POST /trading-permissions/grant` (`confirm: true`). Every order/fill is mirrored into Postgres (`app/trading/persistence.py`) into the real `orders`/`order_events`/`positions`/`trades` tables as it happens — a closing or reducing fill writes a `Trade` journal row (blueprint §61), and the risk engine's exposure check sums real open-position notional. Also: a Redis-backed **trading halt** checked before every order (`account_halt_reason`), real market-data-staleness lookups from the Redis price cache, and **live reconciliation** (`app/trading/live_reconciliation.py`, blueprint §75) — a loop inside the API process's own lifespan (it needs the same `OrderManager`/`PositionManager` instances a user's orders were placed through, which only exist there) that runs `ReconciliationWorker` for every connected account with an active trading stack and halts new entries on any mismatch; resuming is a deliberate manual step, not automatic. `Order`/`Trade` rows carry `strategy_version` (blueprint §91) so a trade always names the exact strategy definition that produced it, even after the strategy is later edited (`PUT /strategies/{id}` bumps `version` rather than overwriting history). |
| 10 | Autonomous trading | **The full loop runs now**, end-to-end, tested against real Postgres/Redis (`tests/workers/test_auto_trade_worker.py`): `ScannerWorker` runs WATCH/SCAN/DETECT; `AutoTradeSupervisor` (`app/workers/auto_trade_worker.py`) runs VALIDATE/RISK CHECK/TRADE/MONITOR/EXIT/JOURNAL for any (user, strategy) pair that has explicitly opted in — `user.auto_trading_enabled` (set only via `POST /auto-trading/enable` with `confirm: true`, blueprint §102) *and* the `AUTO_TRADE` permission *and* the strategy marked both `is_active` and `eligible_for_auto_trading`, checked against the Redis trading halt on every pass. It closes positions, writes a `Trade` journal row, and sends a notification. **This still always drives `MockBroker`** — unlike Stage 9's manual path, `PaperTradingEngine` calls `broker.set_quote(...)` directly to inject each candle's price into the fill simulation, a method only `MockBroker` has; wiring a real broker in here needs the execution flow itself redesigned (a real fill price comes from the broker's own market access, not from the local candle), not just a broker swap — autonomous *paper* trading is real today, autonomous *live* trading is a bigger change than Stage 9's was. |

## Portfolio risk and the correlation engine (§85-86)

Two pieces the blueprint calls out as their own engines, not folded into
`app.risk.engine`'s per-trade checks:

- **`app/risk/correlation.py`** — pure, DB-free math: Pearson correlation
  from real close-to-close returns (`close_returns`, `pearson_correlation`,
  `build_correlation_matrix`), and `correlated_exposure()` summing a
  target position's notional plus every existing position correlated
  with it at or above a configurable threshold. A pair with no computable
  correlation (too little history, zero variance) contributes nothing —
  this only flags concentration it has actual evidence for.
- **`app/risk/portfolio.py`** — the integration layer: `compute_portfolio_exposure`
  reads the real `positions` table for total exposure and a per-market-type
  breakdown (exposed on `GET /portfolio`), and `compute_correlated_exposure`
  fetches each open position's candle history (`app.market.repository.get_candles`)
  to build a real correlation matrix before calling into `correlation.py`.
- **`RiskEngine`** gained a `correlated_exposure_limit` check (blueprint
  §85: "reject a new position when aggregate correlated exposure is too
  high"), wired into `POST /orders`. `RiskLimits.max_correlated_exposure_pct`
  defaults to 100% (a no-op) since correlation data isn't always available
  and this must never silently block trading where it hasn't been computed.

## Broker resolution and live reconciliation (§50, §53, §75)

- **`app/trading/broker_resolver.py`** — the piece blueprint §53 "Broker
  Abstraction" implies but that didn't exist until now: something has to
  pick *which* `Broker` a specific user's orders go through.
  `resolve_broker(db, user)` looks up that user's most recent ACTIVE
  `BrokerAccount`, decrypts its stored credentials, and constructs the
  matching adapter — `UpstoxBroker` for Upstox, `DhanBroker` for Dhan
  (still real code, still ending in `NotImplementedError` on any actual
  call, honestly). No connected account means `MockBroker`. A connected
  account with broken credentials raises rather than quietly falling back
  to Mock.
- **`app/trading/live_reconciliation.py`** — runs `ReconciliationWorker`
  (blueprint §75) for every ACTIVE `BrokerAccount` that also has a live
  trading stack in this process, on a loop started from `app/main.py`'s
  lifespan. It runs inside the **API** process rather than the separate
  `worker` process (see `app/workers/main.py`) because it needs the exact
  `OrderManager`/`PositionManager` instances a user's orders were placed
  through — those live in `app/api/orders.py`'s in-memory `_STACKS`,
  which the `worker` process can never see.

## Multi-leg options execution (§37-40)

`POST /options/execute` takes the legs a client already built via
`POST /options/strategy` (or its own logic) and actually places them:

- **`app/risk/options_risk.py`** — a small, dedicated risk gate, not a
  retrofit of `app.risk.engine.TradeRiskProposal`. A directional trade's
  risk is entry/stop distance; a multi-leg options strategy's risk is
  whatever `app.options.payoff.compute_payoff_summary` already computed
  for the whole combination (`max_loss`, or `capital_requirement` when
  the loss is technically unbounded) — forcing that through an entry/stop
  shape would mean inventing a stop price with no real meaning.
- **Liquidity** is checked per leg against `OptionSnapshot` (via
  `OptionContract.instrument_id`) when one exists — but nothing in this
  codebase populates `option_chains`/`option_contracts`/`option_snapshots`
  yet (no ingestion pipeline exists, the same gap Stage 1 has for a live
  candle feed), so today every leg reports a "no liquidity data available"
  warning rather than ever actually rejecting on real data. That's
  reported honestly in the response (`liquidity_warnings`), not hidden.
- **Not atomic.** Once the strategy-level risk check approves, each leg
  is submitted as its own order through the same broker/persistence path
  `POST /orders` uses (`app.trading.persistence`, the resolved broker from
  `app.trading.broker_resolver`). Neither this codebase nor (unverified)
  Upstox/Dhan guarantee all-or-nothing multi-leg fills, so a later leg's
  rejection does not undo an earlier leg's fill — every leg's own outcome
  is in the response instead of a single pass/fail that would misrepresent
  what actually happened at the broker.

## What's deliberately not implemented

- **Android app** — `android/` is a package-structure scaffold (§6), not a
  working app; it hasn't been built or run (no Android SDK in this
  environment).
- **Real Dhan connectivity** — still a skeleton; see Stage 8. Upstox has a
  real (unverified-against-live-servers) implementation.
- **A live broker tied to a real account balance, verified end-to-end** —
  `app/api/orders.py` (Stage 9) now selects `UpstoxBroker`/`DhanBroker`
  for a user's connected `BrokerAccount` instead of always `MockBroker`
  (see `app/trading/broker_resolver.py`), but that adapter itself is
  still untested against Upstox's real servers (this sandbox's egress to
  upstox.com is blocked — see Stage 8) and Dhan's HTTP calls are still
  `NotImplementedError` TODOs. Autonomous trading (Stage 10) hasn't been
  wired to a real broker at all — see that stage's row for why it's a
  bigger change than Stage 9's was. The SEBI compliance steps in
  `PRODUCTION_READINESS.md` are the remaining non-code blocker either way.
- **Multi-instance coordination beyond Redis** — the API and worker
  processes already share state correctly through Postgres/Redis (see
  "Cross-process design" below), but there's no leader election, so
  running more than one `worker` replica would double-process everything.
- **Multiple API replicas for the manual `/orders` path** — every fill is
  durably mirrored into Postgres now (see Stage 9), but the order state
  machine and position math themselves (`OrderManager`/`PositionManager`,
  held in `app/api/orders.py`'s `_STACKS`) still live in one API
  process's memory. A second API replica would start its own empty
  `_STACKS` and could place a conflicting order for the same user instead
  of seeing the first replica's in-flight state. Rehydrating that state
  from Postgres on startup (or moving it into Redis, like the trading
  halt) is what closes this gap — not attempted here.

## Cross-process design

The API and worker (see `docker-compose.yml`'s `api`/`worker`/`migrate`
services) are separate processes that must agree on shared state without
talking to each other directly:

- **Database engine and Redis client are loop-scoped, not process-global
  singletons** (`app/database/session.py`, `app/core/redis.py`) — each
  holds a connection pool pinned to the asyncio event loop that created
  it, cached per-loop with a `WeakKeyDictionary`. This matters for
  correctness under pytest (a fresh loop per test) as much as for the
  worker process (its own loop, separate from the API's). It also means
  a test's engine/client is never explicitly closed when that test's loop
  is garbage collected — only the Python-side cache entry disappears, not
  the live Postgres/Redis connection underneath it. A real deployment
  never notices (one loop, one engine, for the process's whole life), but
  a full pytest run churns through one loop per test; left unmanaged this
  measurably climbed `pg_stat_activity` from ~9 to 97 (of Postgres's
  default 100 `max_connections`) over one run, silently *skipping*
  (`require_infra`, not failing) whichever tests ran after the limit was
  hit. `tests/conftest.py`'s autouse `_dispose_infra_clients_after_test`
  fixture disposes both at the end of every test, in the same loop, before
  pytest-asyncio tears it down.
- **Trading halts are a Redis flag** (`app.core.redis.halt_account`), not
  an in-memory `KillSwitchState` — a reconciliation mismatch found by the
  `worker` process has to block order placement in the `api` process, so
  the signal has to live somewhere both can see.
- **WebSocket channels are a thin Redis pub/sub relay** (`app/api/websockets.py`)
  — any process (worker or API) can `publish()`, and any connected client
  gets it regardless of which API replica it's attached to.

## Database migrations

`backend/alembic/` holds a real, tested migration history — every
migration round-trips (`upgrade head` / `downgrade -1` / `upgrade head`
again) against Postgres 16, including cleanup for the native enum types
Alembic's autogenerate doesn't drop on its own, and new columns carry a
`server_default` so they apply cleanly against tables that already have
rows. Regenerate with `alembic revision --autogenerate` after changing any
model — see `backend/alembic/env.py`.

## Running the backend

```bash
cd backend
pip install -r requirements.txt
pytest tests   # unit tests always run; integration tests need Postgres/Redis
               # reachable at DATABASE_URL/REDIS_URL and skip themselves if not
alembic upgrade head
uvicorn app.main:app --reload
```

Or via Docker Compose from the repo root: `./scripts/dev_up.sh` (runs
`migrate`, then `api` and `worker`, against `postgres`/`redis` containers).

To exercise the AI features for real, set `AI_PROVIDER=anthropic` and
`AI_API_KEY` in `.env` before starting. CI runs the same test suite
automatically on every push/PR (`.github/workflows/ci.yml`).
