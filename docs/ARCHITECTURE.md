# Implementation Status

This tracks what exists in code against the stages in
`AI_TRADING_PLATFORM_BLUEPRINT.md` §134 ("Project Status Definition").

| Stage | Blueprint area | Status |
|---|---|---|
| 0 | Architecture | Backend scaffolded (`backend/app/*`), repo layout matches §129. Structured logging, request-id tracing, Prometheus `/metrics`, a startup health check (lifespan), and audit logging (`audit_logs`, `risk_events`) are wired in — see §72/§71. |
| 1 | Market data | `app/market`: normalization, timeframe/candle aggregation, simulated feed. `app/workers/market_data_worker.py` + `candle_worker.py` consume a feed, update the Redis latest-price cache, persist closed candles, derive higher timeframes, and publish on `/ws/market` + `/ws/chart`. No *live* broker feed yet — `app/workers/main.py` runs these against `SimulatedFeed` with no data source configured, so the worker process is a real, tested pipeline waiting on a real feed. |
| 2 | SMC/ICT | `app/smc`, `app/ict`: swings, BOS/CHoCH/MSS, liquidity + sweeps, FVG, order blocks, premium/discount, kill zones, opening ranges. Fully unit-tested, look-ahead safe by construction. |
| 3 | Replay | `app/replay`: clock + manual BUY/SELL/SL/TP/CLOSE, statistics. Look-ahead safety proven by test (`tests/replay/test_engine.py`). Exposed over REST (`app/api/replay.py`) with a per-session in-memory store. |
| 4 | Backtesting | `app/backtest`: event loop reusing the same SMC/ICT/Strategy code as replay, configurable cost model, full metrics report, train/validation/test split helper. Persists `backtests`/`backtest_trades`/`backtest_metrics` via `app/api/backtest.py`. |
| 5 | AI | `app/ai`: structured context builder, Strategy-DSL JSON validation, AI trade-proposal validation against deterministic results, deterministic trade explanations. **A real provider is wired**: `app/ai/providers/anthropic_client.py` implements `AIClient` against the Claude API — set `AI_PROVIDER=anthropic` + `AI_API_KEY` to enable it. With no key configured, `NullAIClient` fails closed (§110 "no AI -> no trade"). |
| 6 | Options | `app/options`: Black-Scholes Greeks, multi-leg payoff engine (max profit/loss/breakevens), liquidity filter, named strategy builders (spreads, condor, butterfly, straddle, strangle). |
| 7 | Paper trading | `app/paper`: strategy -> risk -> broker -> position manager -> portfolio, built on the same order/broker stack as live trading. |
| 8 | Dhan/Upstox | `app/brokers/dhan`, `app/brokers/upstox`: adapter skeletons implementing the `Broker` interface. HTTP calls are TODO — see the docstring in each `adapter.py` for the rollout checklist. Do not enable live trading against these until implemented and tested against the brokers' current official docs. |
| 9 | Controlled live trading | `app/api/orders.py` exercises the full risk-gate -> execution -> position flow (currently against `MockBroker` — no live broker wired yet), plus: a Redis-backed **trading halt** checked before every order (`account_halt_reason`), real market-data-staleness lookups from the Redis price cache, and a **reconciliation worker** (`app/workers/reconciliation_worker.py`, blueprint §75) that compares local vs. broker state and halts new entries on any mismatch — resuming is a deliberate manual step, not automatic. |
| 10 | Autonomous trading | Partially started: `app/workers/scanner_worker.py` runs the scan → evaluate → persist-signal → publish loop (blueprint §128's WATCH/SCAN/DETECT stages) on a timer, tested end-to-end against real Postgres/Redis. Still missing: the ANALYZE (AI)/VALIDATE/TRADE/MONITOR/EXIT stages aren't wired into an autonomous loop — a human (or a future worker) still has to act on a signal. |

## What's deliberately not implemented

- **Android app** — `android/` is a package-structure scaffold (§6), not a
  working app; it hasn't been built or run (no Android SDK in this
  environment).
- **Real Dhan/Upstox connectivity** — adapters are structurally complete
  but every HTTP call is a `NotImplementedError` TODO, per the blueprint's
  instruction to always implement against each broker's *current* official
  API docs rather than guessed endpoints. Until one is wired in, the
  worker's market data feed has nothing real to consume.
- **Full autonomous trading loop** — the scanner runs and persists signals,
  but nothing yet turns a signal into an AI-reviewed, risk-checked,
  auto-submitted order end-to-end without a human in the loop.
- **Multi-instance coordination beyond Redis** — the API and worker
  processes already share state correctly through Postgres/Redis (see
  "Cross-process design" below), but there's no leader election, so
  running more than one `worker` replica would double-process everything.

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

`backend/alembic/` holds a real, generated-and-tested initial migration
(`alembic upgrade head` / `downgrade base` round-trips cleanly against
Postgres 16, including the native enum types Alembic's autogenerate
doesn't clean up on its own). Regenerate it with
`alembic revision --autogenerate` after changing any model — see
`backend/alembic/env.py`.

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
`AI_API_KEY` in `.env` before starting.
