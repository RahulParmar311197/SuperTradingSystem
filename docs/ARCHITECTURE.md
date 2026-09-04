# Implementation Status

This tracks what exists in code against the stages in
`AI_TRADING_PLATFORM_BLUEPRINT.md` §134 ("Project Status Definition").
For the honest go/no-go on real money, see
[`PRODUCTION_READINESS.md`](PRODUCTION_READINESS.md) — this file is about
what's built, that file is about what's actually safe to turn on.

| Stage | Blueprint area | Status |
|---|---|---|
| 0 | Architecture | Backend scaffolded (`backend/app/*`), repo layout matches §129. Structured logging, request-id tracing, Prometheus `/metrics`, a startup health check (lifespan), audit logging (`audit_logs`, `risk_events`), and CI (`.github/workflows/ci.yml`, runs the full suite against Postgres/Redis on every push/PR) are wired in — see §72/§71. |
| 1 | Market data | `app/market`: normalization, timeframe/candle aggregation, simulated feed. `app/workers/market_data_worker.py` + `candle_worker.py` consume a feed, update the Redis latest-price cache, persist closed candles, derive higher timeframes, and publish on `/ws/market` + `/ws/chart`. No *live* broker feed yet — `app/workers/main.py` runs these against `SimulatedFeed` with no data source configured, so the worker process is a real, tested pipeline waiting on a real feed. |
| 2 | SMC/ICT | `app/smc`, `app/ict`: swings, BOS/CHoCH/MSS, liquidity + sweeps, FVG, order blocks, premium/discount, kill zones, opening ranges. Fully unit-tested, look-ahead safe by construction. |
| 3 | Replay | `app/replay`: clock + manual BUY/SELL/SL/TP/CLOSE, statistics. Look-ahead safety proven by test (`tests/replay/test_engine.py`). Exposed over REST (`app/api/replay.py`) with a per-session in-memory store. |
| 4 | Backtesting | `app/backtest`: event loop reusing the same SMC/ICT/Strategy code as replay, configurable cost model, full metrics report. **Out-of-sample validation** (`POST /backtest/validate`, blueprint §77-78) runs train/validation/test splits independently and flags overfitting smells (no trades or no edge on the held-out test period, a win-rate collapse from train to validation). Persists `backtests`/`backtest_trades`/`backtest_metrics` via `app/api/backtest.py`. |
| 5 | AI | `app/ai`: structured context builder, Strategy-DSL JSON validation, AI trade-proposal validation against deterministic results, deterministic trade explanations. **A real provider is wired**: `app/ai/providers/anthropic_client.py` implements `AIClient` against the Claude API — set `AI_PROVIDER=anthropic` + `AI_API_KEY` to enable it. With no key configured, `NullAIClient` fails closed (§110 "no AI -> no trade"). |
| 6 | Options | `app/options`: Black-Scholes Greeks, multi-leg payoff engine (max profit/loss/breakevens), liquidity filter, named strategy builders (spreads, condor, butterfly, straddle, strangle). |
| 7 | Paper trading | `app/paper`: strategy -> risk -> broker -> position manager -> portfolio, built on the same order/broker stack as live trading. Now also runs unattended inside the autonomous loop (Stage 10). |
| 8 | Dhan/Upstox | `app/brokers/dhan`: adapter skeleton, every HTTP call still a `NotImplementedError` TODO. **`app/brokers/upstox` is a real implementation** — OAuth2 authorization-code flow (`app/brokers/upstox/oauth.py`, plus `GET /brokers/upstox/authorize` and `/callback`), and `UpstoxBroker` implements every `Broker` method against Upstox's documented v2 API. Built from search-result snippets, not a fetched/verified copy of the live docs (this sandbox's egress to upstox.com is blocked) — tested against a mocked HTTP transport (`tests/brokers/test_upstox_adapter.py`), **never against Upstox's real servers**. Verify every endpoint/field/status-string against the live docs or Postman collection before connecting a real account. |
| 9 | Controlled live trading | `app/api/orders.py` exercises the full risk-gate -> execution -> position flow (currently against `MockBroker` — no live broker wired to a real account balance yet). Gated behind the `LIVE_TRADE` trading permission (blueprint §88), which — like `AUTO_TRADE` — isn't granted at registration; a user opts in via `POST /trading-permissions/grant` (`confirm: true`). Every order/fill is mirrored into Postgres (`app/trading/persistence.py`) into the real `orders`/`order_events`/`positions`/`trades` tables as it happens — a closing or reducing fill writes a `Trade` journal row (blueprint §61) the same way autonomous trading already did, and the risk engine's exposure check now sums real open-position notional instead of a hardcoded zero. Also: a Redis-backed **trading halt** checked before every order (`account_halt_reason`), real market-data-staleness lookups from the Redis price cache, and a **reconciliation worker** (`app/workers/reconciliation_worker.py`, blueprint §75) that compares local vs. broker state and halts new entries on any mismatch — resuming is a deliberate manual step, not automatic. `Order`/`Trade` rows carry `strategy_version` (blueprint §91) so a trade always names the exact strategy definition that produced it, even after the strategy is later edited (`PUT /strategies/{id}` bumps `version` rather than overwriting history). |
| 10 | Autonomous trading | **The full loop runs now**, end-to-end, tested against real Postgres/Redis (`tests/workers/test_auto_trade_worker.py`): `ScannerWorker` runs WATCH/SCAN/DETECT; `AutoTradeSupervisor` (`app/workers/auto_trade_worker.py`) runs VALIDATE/RISK CHECK/TRADE/MONITOR/EXIT/JOURNAL for any (user, strategy) pair that has explicitly opted in — `user.auto_trading_enabled` (set only via `POST /auto-trading/enable` with `confirm: true`, blueprint §102) *and* the `AUTO_TRADE` permission *and* the strategy marked both `is_active` and `eligible_for_auto_trading`, checked against the Redis trading halt on every pass. It closes positions, writes a `Trade` journal row, and sends a notification. **This currently drives `MockBroker` for every account** (see Stage 9) — autonomous *paper* trading is real today; autonomous *live* trading needs a real broker wired to a real account first. |

## What's deliberately not implemented

- **Android app** — `android/` is a package-structure scaffold (§6), not a
  working app; it hasn't been built or run (no Android SDK in this
  environment).
- **Real Dhan connectivity** — still a skeleton; see Stage 8. Upstox has a
  real (unverified-against-live-servers) implementation.
- **A live broker tied to a real account balance** — `UpstoxBroker` can
  authenticate and call every endpoint it implements, but nothing in
  `app/api/orders.py` or the autonomous loop selects it over `MockBroker`
  yet for a specific user's connected `BrokerAccount`. That wiring (plus
  the SEBI compliance steps in `PRODUCTION_READINESS.md`) is what's left
  between "autonomous paper trading" and "autonomous live trading."
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
  worker process (its own loop, separate from the API's).
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
