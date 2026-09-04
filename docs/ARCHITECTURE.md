# Implementation Status

This tracks what exists in code against the stages in
`AI_TRADING_PLATFORM_BLUEPRINT.md` §134 ("Project Status Definition").
For the honest go/no-go on real money, see
[`PRODUCTION_READINESS.md`](PRODUCTION_READINESS.md) — this file is about
what's built, that file is about what's actually safe to turn on.

| Stage | Blueprint area | Status |
|---|---|---|
| 0 | Architecture | Backend scaffolded (`backend/app/*`), repo layout matches §129. Structured logging, request-id tracing, Prometheus `/metrics`, a startup health check (lifespan), audit logging (`audit_logs`, `risk_events`), and CI (`.github/workflows/ci.yml`, runs the full suite against Postgres/Redis on every push/PR) are wired in — see §72/§71. `GET /health` now reports real **worker liveness** (blueprint §117 "Workers 🟢"): each of the `market_data`, `scanner`, and `auto_trade` loops in `app/workers/main.py` refreshes a short-TTL Redis heartbeat every pass (`app.core.redis.heartbeat`), so a stuck or never-started worker process reads `DOWN` honestly instead of an assumed `HEALTHY`. An **admin dashboard** (`app/api/admin.py`, blueprint §115-116) exposes users, broker connections (never `encrypted_credentials`), orders, risk events, AI decisions, this same worker/component health, and — via `GET /admin/halted-accounts` / `POST /admin/accounts/{id}/resume` — the deliberate manual resume step blueprint §75 requires after a reconciliation halt, which previously had no way to happen through the API at all (`app.core.redis.resume_account` existed but nothing ever called it). All gated on `UserRole.ADMIN`. |
| 1 | Market data | `app/market`: normalization, timeframe/candle aggregation, simulated feed. `app/workers/market_data_worker.py` + `candle_worker.py` consume a feed, update the Redis latest-price cache, persist closed candles, derive higher timeframes, and publish on `/ws/market` + `/ws/chart`. No *live* broker feed yet — `app/workers/main.py` runs these against `SimulatedFeed` with no data source configured, so the worker process is a real, tested pipeline waiting on a real feed. |
| 2 | SMC/ICT | `app/smc`, `app/ict`: swings, BOS/CHoCH/MSS, liquidity + sweeps, FVG, order blocks, premium/discount, kill zones, opening ranges. Fully unit-tested, look-ahead safe by construction. |
| 3 | Replay | `app/replay`: clock + manual BUY/SELL/SL/TP/CLOSE, statistics. Look-ahead safety proven by test (`tests/replay/test_engine.py`). Exposed over REST (`app/api/replay.py`) with a per-session in-memory store (`_SESSIONS`) as the live process's working state, but every mutating action now also mirrors into Postgres (blueprint §9's `replay_sessions`/`replay_orders`, previously schema-only tables with zero writers — `app/replay/persistence.py`) so a session survives a restart and a per-user **ownership check** is enforced against a real row rather than trusting any authenticated caller with any session UUID they can guess (`tests/api/test_replay_persistence.py` proves a second user gets a 404, not the first user's state). See "The `.__dict__` bug" below for a real crash this work found and fixed along the way. |
| 4 | Backtesting | `app/backtest`: event loop reusing the same SMC/ICT/Strategy code as replay, configurable cost model, full metrics report. **Out-of-sample validation** (`POST /backtest/validate`, blueprint §77-78) runs train/validation/test splits independently and flags overfitting smells (no trades or no edge on the held-out test period, a win-rate collapse from train to validation). Persists `backtests`/`backtest_trades`/`backtest_metrics` via `app/api/backtest.py`. |
| 5 | AI | `app/ai`: structured context builder, Strategy-DSL JSON validation, AI trade-proposal validation against deterministic results, deterministic trade explanations. **A real provider is wired**: `app/ai/providers/anthropic_client.py` implements `AIClient` against the Claude API — set `AI_PROVIDER=anthropic` + `AI_API_KEY` to enable it. With no key configured, `NullAIClient` fails closed (§110 "no AI -> no trade"). **`POST /ai/propose-trade`** (blueprint §80-81, and §87's "Assisted" user mode — previously entirely unimplemented) asks the AI to confirm a trade for an already-detected setup, validates every stated number against the deterministic `StrategyEngine` result (`app.ai.validation.validate_ai_trade_proposal` — real, tested code that had no caller until now), and persists the outcome as an `AIDecision` row either way (blueprint §71 audit logging explicitly lists "AI decision"; `ai_decisions` was a schema-only table with zero writers before this — `ai_messages` remained one for one more round, see below). It never places an order itself — a validated proposal still goes through `POST /orders` like any other trade. **`POST /ai/chat`** (blueprint §96 "AI Screen") is what finally gives `ai_messages` a writer: a grounded, single-turn Q&A endpoint — the user's message and the AI's reply are both persisted, and when an `instrument_id`/`timeframe` are given the question is answered against real structured facts (`build_ai_prompt_context`), never invented ones. `GET /ai/chat/history` lists a user's own conversation. It is deliberately not a full intent router across every other AI/analysis feature in this file (no "build a strategy from this chat message" auto-dispatch) — each call is independent, with no memory of prior turns fed back in as context. |
| 6 | Options | `app/options`: Black-Scholes Greeks, multi-leg payoff engine (max profit/loss/breakevens), liquidity filter, named strategy builders (spreads, condor, butterfly, straddle, strangle). **Execution exists now too**: `POST /options/execute` (blueprint §37-40) submits every leg of a chosen strategy as real orders — see "Multi-leg options execution" below. |
| 7 | Paper trading | `app/paper`: strategy -> risk -> broker -> position manager -> portfolio, built on the same order/broker stack as live trading. Now also runs unattended inside the autonomous loop (Stage 10). Exposed over REST (`app/api/paper.py`) with a per-session in-memory store (`_SESSIONS`, no test had ever exercised this router before) — `GET`/`POST .../candle` used to have **no ownership check at all**: any authenticated user who knew or guessed another user's session UUID could read its state and feed candles into it, the exact bug already fixed for `/replay/*` in an earlier round but missed here since `PaperTradingEngine.account_id` was never actually checked against the caller. Fixed the same way (404, not 403, for a session that isn't yours), and `DELETE /paper/{id}`/`DELETE /replay/{id}` were added so a session's memory can actually be freed — `_SESSIONS` has no automatic eviction in either router. |
| 8 | Dhan/Upstox | `app/brokers/dhan`: adapter skeleton, every HTTP call still a `NotImplementedError` TODO. **`app/brokers/upstox` is a real implementation** — OAuth2 authorization-code flow (`app/brokers/upstox/oauth.py`, plus `GET /brokers/upstox/authorize` and `/callback`), and `UpstoxBroker` implements every `Broker` method against Upstox's documented v2 API. Built from search-result snippets, not a fetched/verified copy of the live docs (this sandbox's egress to upstox.com is blocked) — tested against a mocked HTTP transport (`tests/brokers/test_upstox_adapter.py`), **never against Upstox's real servers**. Verify every endpoint/field/status-string against the live docs or Postman collection before connecting a real account. |
| 9 | Controlled live trading | `app/api/orders.py` exercises the full risk-gate -> execution -> position flow. **The broker is no longer hardcoded**: `app/trading/broker_resolver.py` looks up the user's connected `BrokerAccount` and returns a real `UpstoxBroker`/`DhanBroker` built from their stored (decrypted) credentials for an ACTIVE connection, or `MockBroker` when nothing is connected — the honest Stage 9 default, not a workaround. A broken connection (missing/malformed stored credentials) raises rather than silently falling back to Mock (blueprint §101: "Never make paper and live look identical"). Gated behind the `LIVE_TRADE` trading permission (blueprint §88), which — like `AUTO_TRADE` — isn't granted at registration; a user opts in via `POST /trading-permissions/grant` (`confirm: true`). Every order/fill is mirrored into Postgres (`app/trading/persistence.py`) into the real `orders`/`order_events`/`positions`/`trades` tables as it happens — a closing or reducing fill writes a `Trade` journal row (blueprint §61), and the risk engine's exposure check sums real open-position notional. Also: a Redis-backed **trading halt** checked before every order (`account_halt_reason`), real market-data-staleness lookups from the Redis price cache, and **live reconciliation** (`app/trading/live_reconciliation.py`, blueprint §75) — a loop inside the API process's own lifespan (it needs the same `OrderManager`/`PositionManager` instances a user's orders were placed through, which only exist there) that runs `ReconciliationWorker` for every connected account with an active trading stack and halts new entries on any mismatch; resuming is a deliberate manual step, not automatic. `Order`/`Trade` rows carry `strategy_version` (blueprint §91) so a trade always names the exact strategy definition that produced it, even after the strategy is later edited — `PUT /strategies/{id}` bumps `version` and now really does snapshot it into `strategy_versions` (`GET /strategies/{id}/versions/{version}` resolves it back), where before this was a comment claiming that but no history table existed at all: an edit just overwrote the same row's `definition` in place, so `strategy_version` on an old trade pointed at a JSON blob that no longer matched what the trade was actually based on. See "Strategy version history" below. |
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

## Strategy version history (§91)

`PUT /strategies/{id}` used to carry a docstring (and this file used to
repeat the claim) saying it "bumps its version rather than overwriting
history" — that was false. The handler bumped `Strategy.version` but then
overwrote that same row's `definition` column in place; no history table
existed anywhere. `Order`/`Trade` rows have carried `strategy_version`
since an earlier round specifically so a trade could be traced back to
the exact DSL that produced it, but there was nothing to trace back *to*
once the strategy was edited again.

Fixed with a new, append-only `strategy_versions` table
(`app.database.models.strategy.StrategyVersion`, unique on
`(strategy_id, version)`): both `POST /strategies` and
`PUT /strategies/{id}` now write one snapshot row the moment a version
comes into existence, and never touch it again. `GET
/strategies/{id}/versions` lists every version; `GET
/strategies/{id}/versions/{version}` resolves one — the second is what
actually answers "what rules produced this trade?" given an `Order`'s or
`Trade`'s `strategy_version`. `tests/api/test_strategy_versions.py` proves
three successive edits leave all three prior definitions independently
readable, and that a second user gets 404 rather than another user's
version history.

## Raw setup journaling (§9)

`setups` (blueprint §9's core table list, distinct from `signals`) is
meant to hold raw SMC pattern detections — structure breaks, fair value
gaps, order blocks — independent of whether any strategy actually matched
on them. It had zero writers anywhere in `app/` before this round: the
same "looks done, is disconnected" bug class as `replay_sessions` and
`strategy_versions` before their fixes.

`ScannerWorker` (`app/workers/scanner_worker.py`) now journals it on every
scan pass, and was restructured to compute SMC/ICT analysis once per
`(instrument, timeframe)` rather than once per `(strategy, instrument)` —
a genuine inefficiency fix along the way, since raw structure detection
never depended on any particular strategy. Persistence is idempotent
across passes: every historical candle gets re-analyzed each time, so
only `(setup_type, detected_at)` combinations not already in the table
get inserted (`tests/workers/test_scanner_worker.py` proves a second pass
over unchanged candles doesn't duplicate rows). `GET /setups` gives it a
real reader too — the kind of thing blueprint §96's "Explain this FVG."
chat prompt would query against.

## Execution mode mislabeling (§101)

`persist_order`/`persist_position`/`record_trade` (`app/trading/persistence.py`)
all take an `execution_mode` parameter, but every call site in
`app/api/orders.py` and `app/api/options.py` (both `POST /orders` and
`POST /options/execute`) called them with no `execution_mode` argument at
all, so every one silently defaulted to `ExecutionMode.LIVE` --
`persist_order` didn't even have a parameter for it; the row was
hardcoded to `LIVE` at construction. This meant **every manual trade ever
placed against `MockBroker`** (Stage 9's honest default for any user with
no connected broker account -- which is every account in this
environment) was journaled in `orders`/`positions`/`trades` as `LIVE`,
exactly the "paper and live look identical" blueprint §101 explicitly
forbids, even though the broker-selection logic itself (see below) was
already doing the right thing.

Fixed by resolving the real execution mode from the stack's actual broker
(`_execution_mode_for`: `PAPER` for `MockBroker`, `LIVE` otherwise) at
every call site, and adding the missing parameter to `persist_order`.
This surfaced a second, dependent bug: `GET /portfolio` computed exposure
via `compute_portfolio_exposure(db, user.id)` with no `execution_mode`
argument either, defaulting to `LIVE` -- which had "worked" by accident
only because positions were always mislabeled `LIVE` too. Once positions
started being labeled correctly, a paper account's real exposure would
have silently gone to zero without also passing the caller's actual mode
through there. Both fixes are proven by test:
`tests/api/test_orders.py`/`test_options_execute.py` assert the persisted
rows are `PAPER` for an account with no connected broker, and the new
`tests/api/test_portfolio.py` (this endpoint had never been tested at
all before) proves `GET /portfolio` still reports real, non-zero exposure
for such an account.

## Portfolio snapshots (§9)

`portfolio_snapshots` (balance/equity/exposure/net Greeks per account) was
another schema-only table with zero writers. `app.trading.portfolio_snapshots.snapshot_all_stacks()`
now journals one row per trading stack that currently has at least one
open position, tagged with the correct `execution_mode` (see "Execution
mode mislabeling" above), reusing `compute_portfolio_exposure` for the
exposure figure and looking up real `OptionSnapshot` rows for net
delta/gamma/theta/vega when one exists for a position's instrument
(contributing 0 when it doesn't -- there's no options-chain ingestion
pipeline in this environment, the same honest gap `app/risk/options_risk.py`
already documents).

This is exposed as `POST /admin/portfolio-snapshot` (ADMIN-only,
on-demand) rather than an automatic background loop. A loop was tried
first, wired into the API process's lifespan the same way
`live_reconciliation` is — and dropped after it destabilized the test
suite: unlike reconciliation, which is bounded by a small, DB-backed set
(`BrokerAccount` rows with `status=ACTIVE`), the candidate set here is
`app.api.orders._STACKS`, which only ever grows for the life of the
process. An immediate on-startup pass over it doesn't stay cheap the way
reconciliation's does — verified firsthand: wiring it into `main.py`'s
lifespan made `pg_stat_activity` climb toward Postgres's connection limit
partway through a full test run (each of ~180 `TestClient(app)` startups
triggered an immediate pass over an ever-growing stack list, some of
which referenced users/instruments already deleted by an earlier test's
own cleanup). Skipping stacks with nothing open right now, and giving
each account processed its own session/commit instead of one shared
transaction for the whole pass, are both real, defensible design choices
on their own — but they weren't sufficient by themselves to make a
background loop safe here, so this stays on-demand until a real
deployment wires it to an external scheduler instead of this process's
own request/response lifecycle.

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

## The `.__dict__` bug

Six places in this codebase built an API response by calling `.__dict__`
on a dataclass instance (`engine.statistics.__dict__`,
`explanation.__dict__`, `metrics.__dict__`, `c.__dict__` for each candle,
`greeks.__dict__`). All six dataclasses are declared `@dataclass(slots=True)`
(or `frozen=True, slots=True`) — and a slotted dataclass has no `__dict__`
attribute at all; Python raises `AttributeError` the instant you touch it.
Every one of these was a **guaranteed 500 on every single call**, not an
edge case:

- `app/api/replay.py` (`_state_response`, feeding every `/replay/*`
  endpoint) and the parallel bug in this change's own new
  `app/replay/persistence.py` (`sync_replay_session`) — caught by the new
  `tests/api/test_replay_persistence.py`, which is what turned up the
  whole bug class.
- `app/api/ai.py`'s `POST /ai/explain-trade` — had never been exercised by
  any test before now (`tests/api/test_ai_propose_trade.py::test_explain_trade_returns_explanation_without_crashing`
  is new).
- `app/api/backtest.py`'s `POST /backtest` (the *run* endpoint, distinct
  from the already-tested `POST /backtest/validate`) — also never
  exercised before now (`tests/api/test_backtest_run.py` is new).
- `app/api/markets.py`'s `GET /candles` — also never exercised before now
  (`tests/api/test_markets_candles.py` is new).
- `app/api/options.py`'s `POST /options/greeks` — found in a later round,
  missed by the original sweep, and (same story) had never been exercised
  by any test before (`tests/api/test_options_greeks.py` is new).

All six were fixed the same way: `dataclasses.asdict(...)` instead of
`.__dict__` (every field involved is a flat scalar/list/dict — no nested
dataclasses — so `asdict`'s recursive conversion is a no-op difference,
just a working one). The real lesson isn't the one-line fix; it's that
five of these six endpoints had shipped through every prior stage of
this project with **zero integration tests actually calling them**, so a
100%-broken code path looked identical to a working one in every test run
until something finally hit it — and even a dedicated sweep for this
exact bug class missed one of the six on the first pass, which is itself
worth remembering: "we already checked for this" is not the same claim as
"we tested every call site."

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
