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
| 7 | Paper trading | `app/paper`: strategy -> risk -> broker -> position manager -> portfolio, built on the same order/broker stack as live trading. Now also runs unattended inside the autonomous loop (Stage 10). Exposed over REST (`app/api/paper.py`) with a per-session in-memory store (`_SESSIONS`, no test had ever exercised this router before) — `GET`/`POST .../candle` used to have **no ownership check at all**: any authenticated user who knew or guessed another user's session UUID could read its state and feed candles into it, the exact bug already fixed for `/replay/*` in an earlier round but missed here since `PaperTradingEngine.account_id` was never actually checked against the caller. Fixed the same way (404, not 403, for a session that isn't yours), and `DELETE /paper/{id}`/`DELETE /replay/{id}` were added so a session's memory can actually be freed — `_SESSIONS` has no automatic eviction in either router. A **manual paper session's closed trades were never journaled anywhere** — `AutoTradeSupervisor` (driving this exact same `PaperTradingEngine`) always wrote a `Trade` row and a notification on close, but `feed_candle` didn't, so `DELETE /paper/{id}` (or a process restart) erased an entire session's realized P&L with no record left behind. Fixed by mirroring `AutoTradeSupervisor._process`'s exact pattern in the route handler. |
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

## `/ws/replay` had no publisher, and no ownership check (§64)

Every other websocket channel (`market`/`chart`/`scanner`/`signals`/
`orders`/`positions`) has a matching `publish()` call somewhere a worker
or API route actually changes that data. `replay` didn't: `step_replay`,
`submit_replay_order`, and `reset_replay` in `app/api/replay.py` all
mutated the engine and persisted it, but never published anything, so a
client connected to `/ws/replay?session_id=...` would authenticate fine,
get a 101 upgrade, and then simply hang forever no matter what the
session did — the only way to see anything was polling `GET
/replay/{id}`. Fixed by having every mutating action publish the same
state `_state_response` already builds.

`ws_replay` also had no ownership check at all — it authenticated the
caller as *some* valid user but never verified they owned `session_id`,
the same class of bug already fixed twice this project for the REST
endpoints (`/replay/*`, `/paper/*`), just missed in the websocket layer.
Fixed the same way: a non-owner's connection is closed (4404) rather than
relayed. `tests/api/test_websockets_replay.py` is new — this project's
first websocket test — and proves both: a step actually broadcasts, and
a second user's connection is rejected while the owner's still works.

## A real Postgres connection leak, finally found (not just the known one)

`tests/conftest.py`'s `_dispose_infra_clients_after_test` fixture (added
several rounds ago) disposes `get_engine()`/`get_redis()` as called from
a **test's own** async context after each test. That's real and still
needed, but writing `tests/api/test_websockets_replay.py` surfaced a
second, larger, previously-misdiagnosed leak behind the same recurring
symptom (`pg_stat_activity` climbing toward Postgres's connection limit
partway through a full local suite run, first blamed — incorrectly — on
this being a long-lived sandbox session in an earlier round's PR).

The real mechanism: `get_engine()`/`get_redis()` cache one client per
*running* event loop. `TestClient(app)` runs the entire ASGI app —
lifespan, every request, the reconciliation background task — on its
**own internal event loop**, and creates a **fresh one on every use**,
even many times within a single outer Python process. Confirmed directly:
repeatedly opening and closing `with TestClient(app):` in a single
process, with no test logic at all beyond `client.get("/health")`, leaked
a brand new cached engine and real, permanently-open Postgres connections
on every single iteration — something no test-loop-scoped fixture could
ever catch, since none of those connections were ever on the *test's*
loop to begin with. `app/main.py`'s lifespan shutdown never disposed
anything at all before this — not a test-only gap, a real one: a
production instance shutting down should release its pool too. Fixed by
disposing both `get_engine()` and `get_redis()` in the lifespan's
shutdown, verified directly (the same repeated-`TestClient` script now
holds Postgres's connection count flat across 10 iterations instead of
leaking 2 per iteration), and by two full local `pytest tests -q` runs
(189 passed, both times) where the exact same run had been intermittently
failing 8-15 tests with `TooManyConnectionsError` before this fix.

## Notifications (§63, §104)

`app.notifications.service.create_notification` is real, tested code
called from real worker paths (`app/workers/reconciliation_worker.py`,
`app/workers/auto_trade_worker.py`) — every reconciliation halt and every
autonomous trade actually writes a `notifications` row. But there was no
API endpoint anywhere to read them back: `app/main.py`'s router list
included every other domain module except this one. The rows were real,
persisted, and permanently unreachable by any client — a write-only
table, the inverse of the "table exists, nothing writes to it" bug found
repeatedly elsewhere this project.

`GET /notifications` (optional `unread_only`) and `PATCH
/notifications/{id}/read` (`app/api/notifications.py`, new) fix this,
with the same ownership check every per-user resource in this codebase
now gets: another user's notification 404s rather than 403ing.
`tests/api/test_notifications.py` is new — this was, like several other
fixes this project, completely without test coverage before.

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

**Correction from a later round:** the diagnosis above was real but
incomplete. The dominant leak wasn't specific to this feature's extra
per-`TestClient` work at all — it was `app/main.py`'s lifespan never
disposing `get_engine()`/`get_redis()` on shutdown, a bug present the
whole time that this feature's extra work just made visible faster (more
work per `TestClient` cycle before its portal loop closed and its
connections were abandoned). See "A real Postgres connection leak,
finally found" below for the actual fix. This background-loop-vs-on-demand
tradeoff is still the right call on its own merits (`_STACKS` genuinely
never shrinks), just not for the reason originally given as the primary
one.

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

## A TOCTOU race in per-user trading stack creation

`app/api/orders.py`'s `_stack_for(user, db)` is the lookup-or-create for a
user's `_UserTradingStack` (its `OrderManager`/`PositionManager`/`RiskEngine`
and resolved `Broker`) in the in-memory `_STACKS` registry described above.
Before this fix it read:

```python
if user.id not in _STACKS:
    broker = await resolve_broker(db, user)
    _STACKS[user.id] = _UserTradingStack(broker)
return _STACKS[user.id]
```

The check and the write straddle a real `await` — `resolve_broker` runs a
`BrokerAccount` query against Postgres — with nothing serializing access in
between. Two concurrent first calls for the same user (two browser tabs
opening right after login, or a frontend firing `GET /positions` and
`POST /orders` back to back) can both observe `user.id not in _STACKS` as
`True` before either finishes resolving its broker. Each then builds its
own `_UserTradingStack`; whichever write lands second silently replaces the
first in the dict. If the first request had already placed an order through
its (now-discarded) stack — the order itself is still correctly persisted
to Postgres via `app.trading.persistence`, that part never depended on
`_STACKS` — every subsequent call in that process resolves to the *second*
stack's `OrderManager`/`PositionManager`, which never saw it. Since
`GET /orders`, `GET /positions`, and `GET /portfolio` all read the in-memory
managers rather than re-querying Postgres, that order becomes permanently
invisible through the API for the rest of the process's life, and a later
`POST /orders/{id}/cancel` on it 404s even though it genuinely exists in the
database. This is the same class of bug as the ones already fixed for
`/paper/*` and `/replay/*` (missing ownership checks) in that it's an
in-memory-registry correctness gap invisible to any test that only exercises
one request at a time — every existing test in `tests/api/test_orders.py`
issues requests sequentially through a single `TestClient`, so none of them
could have caught it.

Fixed with standard double-checked locking, keyed per user so unrelated
users' stack creation never blocks on each other:

```python
_STACK_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}

async def _stack_for(user: User, db: AsyncSession) -> _UserTradingStack:
    if user.id not in _STACKS:
        lock = _STACK_LOCKS.setdefault(user.id, asyncio.Lock())
        async with lock:
            if user.id not in _STACKS:
                broker = await resolve_broker(db, user)
                _STACKS[user.id] = _UserTradingStack(broker)
    return _STACKS[user.id]
```

`dict.setdefault` for the lock itself needs no additional protection: plain
dict access between two `await` points can't interleave within a single
event loop, so two coroutines calling `setdefault` "concurrently" (i.e.
back to back with no `await` in between) always agree on the same `Lock`
instance. `tests/api/test_orders.py::test_concurrent_stack_for_calls_share_one_stack`
proves the fix directly — it monkeypatches `resolve_broker` to sleep before
resolving (widening the race window on demand rather than relying on timing
luck) and asserts two concurrent `_stack_for` calls for the same user return
the *same* stack object; reverting the lock reproduces the failure
immediately (verified by hand before committing this fix).

## Autonomous trading kept running an edited strategy's *old* version

`app/workers/auto_trade_worker.py`'s `AutoTradeSupervisor._process` caches
one `PaperTradingEngine` per `(user_id, strategy_id, instrument_id)` in
`self._engines`, built once from whatever `StrategyDefinition` existed at
that moment. `run_once` re-fetches the `StrategyRow` and re-parses a fresh
`StrategyDefinition` from its *current* `definition` column on every single
pass — but `_process` only ever used that freshly-parsed object on a cache
miss (`if engine is None`). On a cache hit, it was silently discarded, and
`PaperTradingEngine.strategy` (set once in `__init__`, never reassigned
anywhere in `app/paper/engine.py`) kept driving every future candle against
whatever DSL existed the moment the engine was first built.

Concretely: a user calls `PUT /strategies/{id}` to edit a strategy that's
already running under auto-trading. This bumps `strategy_row.version` and
rewrites `strategy_row.definition` in Postgres — but the already-running
engine for that `(user, strategy, instrument)` key kept trading the *old*
logic indefinitely, until the process happened to restart. Worse than
simply stale: the `Trade` row journaled on close still stamped
`strategy_version=strategy_row.version` — the *current*, edited version
number — even though the trade was actually produced by the old DSL. The
audit trail (`strategy_versions`, the whole point of blueprint §91's
versioning table) pointed at the wrong definition, not just an outdated
one. The same staleness applied to auto-trading risk limits
(`risk_per_trade_pct`/`max_daily_loss_pct`/`max_trades_per_day`/
`max_open_positions`, set via `POST /auto-trading/enable`): once an engine
was built, those never refreshed either.

This is the third instance of the same underlying pattern already fixed
twice elsewhere in this project — a background worker and a manual/API
code path (or, here, the worker's own re-fetch-vs-cache logic) diverging on
which state is actually live. Fixed by tracking the strategy version each
cached engine was last built or updated against
(`self._engine_strategy_versions`) and, on a cache hit where the current
`strategy_row.version` differs, swapping `engine.strategy` in place rather
than discarding the fresh definition — deliberately *not* rebuilding the
whole engine, since that would also reset its `MockBroker` balance and
silently close out any currently open position. Risk limits are cheaper to
get right: they're reconstructed from the user's current settings on every
single pass, cache hit or not, since a plain `RiskLimits` dataclass swap has
no state to lose.

`tests/workers/test_auto_trade_worker.py::test_supervisor_picks_up_strategy_edited_after_engine_cached`
proves the fix: it starts a strategy with a condition that can never match
the test's bullish dataset (forcing a cache-building pass that produces no
trade), edits the strategy in place to the real matching definition and
bumps its version exactly like `PUT /strategies/{id}` would, then feeds the
rest of the same dataset and asserts a trade still opens, closes, and is
journaled with the *new* `strategy_version` — which is only possible if the
already-cached engine actually picked up the edit. Reverting the fix
reproduces the failure immediately (verified by hand before committing).

## No API path could ever promote a strategy to `eligible_for_auto_trading` (§77)

`strategies.eligible_for_auto_trading` defaults to `False`
(`app/database/models/strategy.py`), and `AutoTradeSupervisor.run_once`
(`app/workers/auto_trade_worker.py`) only considers strategies matching
`WHERE is_active IS TRUE AND eligible_for_auto_trading IS TRUE`. Before this
fix, nothing in the API ever wrote that column: `POST /strategies`
(`create_strategy`) left it at the DB default, and `PUT /strategies/{id}`
(`update_strategy`) only ever touched `definition`/`name`/`version` — its
request body is the raw `StrategyDefinition` DSL model, which has no field
for it at all. The only place in the entire repository that ever set
`eligible_for_auto_trading=True` was test fixtures constructing a
`StrategyRow` directly against the database, bypassing the API entirely.

The practical effect: regardless of a user completing the account-level
`POST /auto-trading/enable` switch (with `confirm: true`, the AUTO_TRADE
permission, and sane risk limits), `strategy_rows` in
`AutoTradeSupervisor.run_once` was guaranteed empty for every real account,
so the entire autonomous order-placement/risk-check/journaling path —
Stage 10 of the blueprint, the feature `AutoTradeSupervisor` exists to
implement — was unreachable dead code in production. Blueprint §77
describes exactly the missing piece: a strategy is meant to graduate
through `Backtest → Out-of-sample → Replay → Paper trading → Risk review →
Limited live deployment` before being marked eligible for autonomous
trading; that graduation step didn't exist anywhere.

Fixed by adding `PATCH /strategies/{id}/status`
(`app/api/strategies.py:update_strategy_status`) — deliberately a separate
endpoint from `PUT /strategies/{id}` (editing the DSL) rather than folding
eligibility into it, so promoting a strategy to autonomous trading is never
an incidental side effect of an unrelated definition edit. Turning
`eligible_for_auto_trading` **on** requires the `AUTO_TRADE` permission
(403 without it) and an explicit `confirm: true` (400 without it) — the
same two-gate pattern `POST /auto-trading/enable` already uses for the
account-level switch. Turning it **off**, and toggling `is_active` in
either direction, needs neither: demoting a strategy or deactivating it
must never be harder than promoting it, the same asymmetry
`POST /auto-trading/disable` already establishes as a kill-switch
principle. Every status change is audit-logged
(`action="strategy.status_updated"`).

`tests/api/test_strategy_status.py` proves the gating end to end:
promoting without the `AUTO_TRADE` permission is rejected (403), promoting
with the permission but without `confirm` is rejected (400), a correctly
confirmed promotion actually persists, and demoting/deactivating need
neither check. `tests/workers/test_auto_trade_worker.py`'s existing
`test_supervisor_opens_and_journals_a_trade_end_to_end` already proves the
supervisor half of the pipeline works correctly once
`eligible_for_auto_trading` is `True` — this fix is what makes reaching
that state through the real API possible at all, closing the loop between
the two.

## A disconnected broker was silently swallowed instead of halting the account (§74)

`ReconciliationWorker.run_once()` (`app/workers/reconciliation_worker.py`)
already handles one half of blueprint §74 "Broker Failure Handling": if
local and broker state both come back successfully but *disagree*, it
halts the account, records an audit entry, and notifies the user. It never
handled the other half — the broker being unreachable at all. `get_orders`/
`get_positions` were called with no `try`/`except` around them at all. A
real adapter doesn't return stale or empty data when it can't reach the
broker — it raises. `UpstoxBroker._get` calls `response.raise_for_status()`
unconditionally, so an expired/revoked token, a network failure, or an
outage surfaces as `httpx.HTTPStatusError`/`httpx.HTTPError`; `DhanBroker`'s
methods still end in `NotImplementedError` (honestly, per its own
docstring). Every adapter's own `is_healthy()` already treats exactly this
set — `(BrokerError, httpx.HTTPError, NotImplementedError)` — as "not
healthy"; `run_once` was the one place that didn't.

That exception propagated straight out of `run_once` and up into
`app/trading/live_reconciliation.py`'s `reconcile_all_connected_accounts`,
which wraps each account's pass in a bare `except Exception:
logger.exception(...)`. The result: a genuinely disconnected broker —
exactly the failure §74 exists to handle — was silently logged and
forgotten every reconciliation cycle. The account was never halted
(`account_halt_reason` stayed `None`, so `POST /orders` kept accepting new
live entries against a broker that had just proven unreachable), no
notification reached the user, and `BrokerAccount.status` never left
`ACTIVE` (confirmed by grep: nothing in the codebase ever writes any other
status to that column). `NotificationType.BROKER_DISCONNECTED` had been
defined in the model since early on and was never emitted anywhere — the
same class of dead-value bug as `eligible_for_auto_trading` above, just in
a notification type instead of a boolean column.

Fixed by wrapping the two broker calls in `run_once` in
`try`/`except (BrokerError, httpx.HTTPError, NotImplementedError)` and, on
failure, doing exactly what the mismatch branch already does — `halt_account`,
`record_audit` (`action="reconciliation.broker_unreachable"`), and a
`BROKER_DISCONNECTED` notification — then returning a `ReconciliationReport`
carrying the failure as its own mismatch entry rather than propagating the
exception (so a caller checking `report.in_sync` still gets a truthful
answer instead of an unhandled exception).
`tests/workers/test_reconciliation_worker.py::test_reconciliation_halts_account_when_broker_is_unreachable`
proves it: a broker stub whose `get_orders` raises `BrokerError` (unlike
`MockBroker.set_healthy(False)`, which only changes what `is_healthy()`
reports, never what `get_orders`/`get_positions` do) still results in the
account being halted, a `BROKER_DISCONNECTED` notification, and the audit
entry — reverting the fix reproduces the unhandled-exception failure
immediately (verified by hand before committing).

## A risk-rejected paper/auto-trade entry left no record anywhere (§63)

`PaperTradingEngine.on_candle` (`app/paper/engine.py`) returns a
`PaperTradeOutcome` whose `risk_rejected_reason` field is set whenever a
matched entry signal gets vetoed by `RiskEngine.evaluate` — daily loss
limit, max open positions, correlated exposure, any of the checks in
`app/risk/engine.py`. Both of this engine's two callers computed that
value and then simply never read it: `app/api/paper.py`'s `feed_candle`
discarded it (`PaperStateResponse` doesn't even have a field for it), and
`app/workers/auto_trade_worker.py`'s `_process` discarded it too. A
rejected entry vanished the instant `on_candle` returned — not a
notification, not an audit-log entry, nothing queryable anywhere. This is
the same "a value is computed but nothing ever consumes it" shape as the
two previously-fixed dead-value bugs (`eligible_for_auto_trading`,
`BROKER_DISCONNECTED`), and blueprint §63 explicitly lists "Order
rejected" as a mandatory notification event.

The autonomous path made this worse than the manual one: `POST /orders`'s
own risk rejection at least returns a synchronous `403` with
`decision.reason` in the body, so a manual live-trading user always sees
why their order didn't go through. `AutoTradeSupervisor` has no HTTP
response for anyone to read — it runs on a timer, unattended — so a
rejected autonomous entry left literally zero trace the account's owner
could ever discover, unless they happened to notice a signal that should
have opened a position simply never did.

Fixed by mirroring each call site's existing `order_created` branch: when
`risk_rejected_reason` is set, `record_audit` (`paper.order_rejected` /
`autotrade.order_rejected`) and `create_notification` with
`NotificationType.ORDER_REJECTED`, body set to the rejection reason
itself. `tests/api/test_paper.py::test_paper_trading_notifies_on_risk_rejected_entry`
and `tests/workers/test_auto_trade_worker.py::test_supervisor_notifies_on_risk_rejected_entry`
both monkeypatch `RiskEngine.evaluate` to force a deterministic rejection
on a dataset already proven (in sibling tests) to otherwise open and close
a position, then assert the notification and audit entry land with the
forced reason. Reverting either fix reproduces the original silent
swallowing immediately (verified by hand before committing).

A repo-wide check of every `NotificationType` enum value confirms 3 of 11
are still never triggered anywhere — `SETUP_DETECTED`, `TRADE_EXECUTED`,
`SL_HIT`/`TP_HIT` (closing a position emits `POSITION_CLOSED` instead, not
separately, which arguably already covers the same event), `DAILY_LOSS_LIMIT`,
`MARKET_DATA_STALE`, and `AUTO_TRADING_DISABLED` remain unwired. Each is a
real, separately-scoped gap in its own right (a scanner-side setup
detection, an order-fill completion event, the market-data-freshness halt
path, and an auto-trading kill-switch notification are four different
subsystems) — left for a future round rather than folded into this one, to
keep this fix's diff and its tests focused on the single concrete case
already reproduced above.

## SL/TP hits fired a generic notification instead of their own (§63)

`PaperTradingEngine._maybe_exit` (`app/paper/engine.py`) decides whether a
candle closes an open position by branching explicitly on which side of
the bracket the price crossed — a long's stop is `candle.low <= stop`, its
target `candle.high >= target` (and the mirror image for a short). It
*knows* which one fired to pick the right `exit_price` — that information
just never left the function. `PaperTradeOutcome` only carried a bare
`closed_position_pnl: float | None`, so both callers that consume it
(`app/api/paper.py`'s `feed_candle`, `app/workers/auto_trade_worker.py`'s
`_process`) always fired the same generic `NotificationType.POSITION_CLOSED`
for every close, whether it was a stop-loss or a take-profit. A grep for
`SL_HIT`/`TP_HIT` across `app/` before this fix turned up nothing but the
two enum declarations — not just unwired, like `BROKER_DISCONNECTED` and
`eligible_for_auto_trading` before them, but the underlying signal was
computed and thrown away at the one place in the codebase that actually
had the answer.

Fixed by adding `PaperTradeOutcome.exit_reason: Literal["stop_loss",
"take_profit"] | None`, having `_maybe_exit` return it alongside `pnl`
(both are `None` together — nothing closed) and `on_candle` pass it
through unchanged. Both call sites now pick
`NotificationType.SL_HIT`/`TP_HIT` from a small dict keyed on
`exit_reason`, falling back to `POSITION_CLOSED` only if `exit_reason` is
`None` — which can't currently happen when `closed_position_pnl` is set,
but the fallback costs nothing and doesn't assume that invariant holds
forever.

`tests/api/test_paper.py::test_paper_trading_notifies_sl_hit_on_stop_loss_exit`
and `tests/workers/test_auto_trade_worker.py::test_supervisor_notifies_sl_hit_on_stop_loss_exit`
are new: each is the mirror image of an existing "runs hard to target"
dataset — same bullish FVG setup, but reversing hard through the stop
instead — asserting a negative-P&L `Trade` row and an `SL_HIT` (not
`POSITION_CLOSED`) notification. The existing take-profit end-to-end tests
in both files were also strengthened to assert `TP_HIT` specifically.
Reverting the fix (dropping `exit_reason` from the return value) reproduces
the original generic notification immediately on both new tests (verified
by hand before committing).

Two of the six previously-flagged unwired `NotificationType` values
(`SETUP_DETECTED`, `TRADE_EXECUTED`, `DAILY_LOSS_LIMIT`, `MARKET_DATA_STALE`,
`AUTO_TRADING_DISABLED` are the remaining four) are resolved by this PR.
The rest stay open for a future round — each spans a different subsystem
(scanner-side detection, an order-fill completion event, the market-data
freshness halt path, and an auto-trading kill-switch) and deliberately
isn't folded into this fix.

## Opening a trade never notified anyone (§63)

Blueprint §63 lists "Trade executed" as its own notification event,
alongside "Order rejected", "Position closed", "SL hit", and "TP hit" — all
of which were wired by the previous two rounds. `TRADE_EXECUTED` itself
was still dead: `app/api/paper.py`'s `feed_candle` and
`app/workers/auto_trade_worker.py`'s `_process` both only called
`record_audit` inside their `if outcome.order_created:` branch, never
`create_notification`. Every sibling branch in both functions —
`risk_rejected_reason` (`ORDER_REJECTED`) and `closed_position_pnl`
(`SL_HIT`/`TP_HIT`) — already notified; opening a position was the one
event in the whole lifecycle that stayed silent. A user running manual
paper trading or autonomous auto-trading got notified when an entry was
rejected and when a position closed, but nothing at all when a trade
actually opened — `GET /notifications` would never return a
`TRADE_EXECUTED` row no matter how many trades ran.

Fixed by adding a `create_notification(NotificationType.TRADE_EXECUTED,
...)` call right after the existing `record_audit` in both
`order_created` branches, matching the wording style of the adjacent
`ORDER_REJECTED` blocks. This is the fourth and, for the notification
system specifically, the last of the previously-flagged unwired
`NotificationType` values within this trading lifecycle — `SETUP_DETECTED`,
`DAILY_LOSS_LIMIT`, and `MARKET_DATA_STALE`/`AUTO_TRADING_DISABLED` are
different subsystems (scanner-side detection, the risk engine's own daily
check, and the market-data-freshness/kill-switch paths respectively) and
remain open for future rounds.

Every existing end-to-end test that opens and then closes a position in
`tests/api/test_paper.py` and `tests/workers/test_auto_trade_worker.py`
was updated to assert exactly two notifications now land per full
open-then-close cycle (`TRADE_EXECUTED` plus whichever close type fired),
instead of the previous one — these updated assertions are the regression
tests: reverting the new `create_notification` calls reproduces the
missing `TRADE_EXECUTED` row immediately (verified by hand before
committing), failing four tests across both files.

## A daily-loss-limit rejection looked like any other rejection (§63)

`RiskEngine.evaluate` (`app/risk/engine.py`) already computes the daily
loss check as its own named `RiskCheck("daily_loss_limit", daily_loss_pct
< limits.max_daily_loss_pct, f"Daily loss {daily_loss_pct:.2f}% vs limit
{limits.max_daily_loss_pct}%")`, one of eleven checks it evaluates in
order. But `RiskDecisionResult` (`app/risk/limits.py`) only ever surfaced
that as a single collapsed `reason` string (the first failed check's
`detail` or `name`) — nothing preserved *which* check it was as a stable,
matchable identity. `PaperTradingEngine.on_candle` then passed that bare
string straight into `PaperTradeOutcome.risk_rejected_reason`, so by the
time either caller (`app/api/paper.py`'s `feed_candle`,
`app/workers/auto_trade_worker.py`'s `_process`) saw the rejection, there
was no reliable way to tell a daily-loss-limit veto apart from a
max-open-positions veto, a correlated-exposure veto, or a stale-market-data
veto — every one fired the same generic `NotificationType.ORDER_REJECTED`.
Blueprint §63 lists "Daily loss limit" as its own distinct notification
event, separate from a generic order rejection; `DAILY_LOSS_LIMIT` sat
unused in the enum the same way `BROKER_DISCONNECTED` and
`eligible_for_auto_trading` once did.

Fixed by adding `PaperTradeOutcome.risk_failed_check: str | None` — the
failed check's `name` (e.g. `"daily_loss_limit"`), taken from
`RiskDecisionResult.failed_checks[0].name` (a property that already
existed) rather than parsing the free-text `reason`. Both call sites now
pick `NotificationType.DAILY_LOSS_LIMIT` when `risk_failed_check ==
"daily_loss_limit"`, falling back to `ORDER_REJECTED` for every other
kind of veto — the same small-dict/ternary dispatch pattern already used
for `SL_HIT`/`TP_HIT` a few sections above.
`tests/api/test_paper.py::test_paper_trading_notifies_daily_loss_limit_distinctly`
and `tests/workers/test_auto_trade_worker.py::test_supervisor_notifies_daily_loss_limit_distinctly`
each monkeypatch `RiskEngine.evaluate` to return a rejection whose
`checks` list contains a failed `RiskCheck("daily_loss_limit", ...)` (as
opposed to the pre-existing generic-rejection tests, which pass an empty
`checks` list and still correctly fall back to `ORDER_REJECTED`) and
assert the resulting notification is `DAILY_LOSS_LIMIT` specifically.
Reverting the dispatch logic in either call site reproduces the generic
notification immediately (verified by hand before committing).

Three `NotificationType` values remain unwired after this round:
`SETUP_DETECTED`, `MARKET_DATA_STALE`, and `AUTO_TRADING_DISABLED` — each
a different subsystem (scanner-side pattern detection, the market-data
freshness halt path, and an auto-trading kill-switch respectively),
deliberately left open for future rounds rather than folded into this fix.

## Live order rejections were the one path that never notified (§63)

Three code paths reject a proposed trade against the risk engine's
verdict: manual paper trading (`app/api/paper.py`'s `feed_candle`),
autonomous trading (`app/workers/auto_trade_worker.py`'s `_process`), and
manual live/mock trading (`app/api/orders.py`'s `place_order`). The first
two were fixed in earlier rounds to dispatch `NotificationType.ORDER_REJECTED`
or `DAILY_LOSS_LIMIT` (depending on which `RiskCheck` failed) whenever
`RiskEngine.evaluate` rejects. `place_order` — the one path that places a
live order against a real connected broker (or `MockBroker` when none is
connected) — never got the same treatment. It wrote a `RiskEvent` audit
row and raised an `HTTP 403` back to the caller, and that was it:
`app/api/orders.py` didn't even import `create_notification` or
`NotificationType`.

Concretely, this meant the highest-stakes rejection path in the whole
system — a user's live order blocked by their daily loss limit, exposure
cap, correlated-exposure limit, or stale market data — left no
`Notification` row anywhere. The synchronous `403` response told whichever
HTTP client made that specific call, and nothing else: `GET /notifications`
would never show it, a second device or browser tab would never learn
about it, and no admin view could see it happened. The inconsistency with
the two already-fixed sibling paths made this an obvious gap once found:
same trigger, same `RiskDecisionResult`, same missing notification.

Fixed by mirroring the exact dispatch already used in `app/api/paper.py`
and `app/workers/auto_trade_worker.py` — `NotificationType.DAILY_LOSS_LIMIT`
when `decision.failed_checks[0].name == "daily_loss_limit"`, else
`ORDER_REJECTED` — added to `place_order`'s existing `if not
decision.approved:` branch, right before the `HTTPException` is raised
(the audit trail was already covered by the `RiskEvent` row written just
above it; this only adds the missing user-facing notification).
`tests/api/test_orders.py` adds two tests mirroring the equivalent
paper/auto-trade regression tests — one monkeypatching `RiskEngine.evaluate`
to a generic rejection (asserting `ORDER_REJECTED`), one to a rejection
whose `checks` list contains a failed `daily_loss_limit` check (asserting
`DAILY_LOSS_LIMIT` specifically). Reverting the fix reproduces the missing
notification immediately (verified by hand before committing).

Three `NotificationType` values remain unwired: `SETUP_DETECTED`,
`MARKET_DATA_STALE`, and `AUTO_TRADING_DISABLED` — different subsystems
(scanner-side pattern detection, the market-data freshness halt path, and
an auto-trading kill-switch respectively), left open for future rounds.

## A timing side channel in login let an attacker enumerate accounts

`app/auth/service.py`'s `login()` used to read:

```python
user = await get_user_by_email(db, email)
if user is None or not verify_password(password, user.password_hash):
    ...
    raise AuthError("Invalid email or password")
```

Python's `or` short-circuits, so a login attempt for an email with no
matching account returned after only a Postgres `SELECT` — fast — while an
attempt for a real, registered email always paid the cost of
`bcrypt.checkpw` inside `verify_password` (`app/auth/security.py`),
deliberately slow (tens of milliseconds, by design — that's what makes
bcrypt resistant to offline brute-forcing). That asymmetry is a textbook
timing side channel: an unauthenticated caller can distinguish "this email
is registered" from "this email is not registered" purely from response
latency, with no dependency on guessing the password at all. `POST
/auth/login`'s rate limit (`app/api/auth.py`, 10/minute per key) slows this
down but doesn't close it — it bounds the query rate, not what a single
response's timing leaks, and an attacker can spread a target list across
many keys or simply wait. `POST /auth/register` (`AuthError("An account
with this email already exists")`) is a separate, already-visible
enumeration channel by design — a 409 conflict is an explicit, documented
response, not a side channel — and is out of scope here; this fix is about
login's timing leak specifically.

Fixed by always calling `verify_password` exactly once, on a real bcrypt
hash either way: the user's own hash when the account exists, or a
precomputed dummy hash (`DUMMY_PASSWORD_HASH` in `app/auth/security.py`,
computed once at import time via `hash_password(...)` on an arbitrary
string with no matching account) when it doesn't. Both branches now do the
same amount of work regardless of which is true, so response latency no
longer reveals whether the submitted email is registered.
`tests/api/test_auth_login.py::test_login_with_unknown_email_still_pays_bcrypt_cost`
proves it by spying on `verify_password` (rather than asserting on wall-clock
timing, which would be flaky under CI load) — it asserts the function is
still called exactly once, against `DUMMY_PASSWORD_HASH`, for a login
attempt against an email with no account. Reverting the fix reproduces the
short-circuit (spy never called) immediately (verified by hand before
committing). Two more tests in the same file cover the ordinary
correct/incorrect-password paths to confirm behavior is otherwise
unchanged.

## `ENVIRONMENT` was declared but read nowhere — production could boot with a public encryption key

`Settings.credentials_encryption_key` (`app/core/config.py`) has always
defaulted to a real, working Fernet key — not an obviously-invalid
placeholder the way `jwt_secret`'s default (`"change-me-in-production"`)
is. This key encrypts every connected broker account's OAuth credentials
before they're persisted (`app/api/brokers.py` → `BrokerAccount.encrypted_credentials`,
decrypted in `app/trading/broker_resolver.py`). Because it's committed to
source, it's public by construction — anyone who has ever cloned this
repository, or found it on GitHub, already has it.

Nothing enforced that a real deployment actually overrode it before
serving traffic. `docs/PRODUCTION_READINESS.md` already told operators to
"generate real values, don't ship the repo's dev defaults" — good advice,
but advice only, with no code checking it was followed. Worse: `Settings`
had an `environment` field (`"development"` by default) that looked like
exactly the right place to gate a "you're in production, so this must be
configured" check — except a repo-wide grep confirmed `settings.environment`
was never read anywhere in the entire codebase. It was pure decoration:
declared, defaulted, and otherwise completely inert. A misconfigured
production deployment — someone who copied `.env.example`, forgot to fill
in `CREDENTIALS_ENCRYPTION_KEY`, or simply never knew that field's default
was a real key rather than an invalid placeholder — would boot silently
and serve traffic indefinitely with a publicly-known secret protecting
every user's broker credentials.

Fixed with a `pydantic` `model_validator(mode="after")` on `Settings`
(`_refuse_default_secrets_in_production`) that raises a clear `ValueError`
at construction time — meaning at process startup, since `get_settings()`
constructs `Settings()` eagerly — whenever `environment == "production"`
and either `jwt_secret` or `credentials_encryption_key` is blank or still
equals its repository default. Blank is checked as its own case, not just
equality with the default: `.env.example` ships
`CREDENTIALS_ENCRYPTION_KEY=` empty by design (to force an operator to
notice it), and pydantic-settings treats an empty value in `.env` as an
explicit override to `""`, not "fall through to the class default" — so
an operator who copies the file and simply forgets to fill it in would
sail right past a check that only compared against the committed default
string. This is the first time `environment` does anything at all in this
codebase; `.env.example` and `docs/PRODUCTION_READINESS.md` were both
updated to actually instruct setting `ENVIRONMENT=production`, since
nothing previously told an operator that field mattered.

`tests/test_core_config.py` proves all of this: development settings
(the class defaults, or whatever a local `.env` overrides, `environment`
left at its own default) never raise, since the entire point is that local
dev and the test suite work with zero configuration; `environment=
"production"` with either secret at its default, or blank, raises with a
message naming the specific variable to set; `environment="production"`
with real values for both starts normally. Reverting the validator
reproduces the silent-boot behavior immediately (verified by hand before
committing).

## No way to log out (§69)

Blueprint §69's Authentication list is `JWT access token / Refresh token /
Password hashing / Session management / Device tracking`. The first three
existed from early on; the last two didn't, despite `UserSession`
(`app/database/models/users.py`) already carrying exactly the columns
"session management" and "device tracking" need — `revoked`, `device_info`,
`expires_at`. `revoked` was only ever set to `True` in one place:
`auth_service.refresh()`, as a side effect of rotating a used refresh
token. No user action — logging out, revoking a specific device, reacting
to a suspected compromise — could set it. A stolen refresh token, or a
forgotten logged-in shared computer, stayed valid until its multi-day
natural expiry (`refresh_token_expire_days`) with zero self-service
remediation. `device_info` was written at every login and never read back
by anything — collected, but write-only, making "device tracking" nothing
more than an unused column.

Fixed by adding three endpoints to `app/api/auth.py`:
- **`POST /auth/logout`** — takes a `refresh_token` (same shape as
  `/auth/refresh`, deliberately with no bearer-auth dependency, since a
  user whose access token already expired but whose refresh token is
  still live must still be able to log out) and revokes the session it
  maps to. Lenient by design: an already-revoked session, an already-
  rotated-out token, or outright garbage all resolve to `204` rather than
  an error — the caller wanted to be logged out, and after this call, they
  are. Same "a kill switch must never be harder to reach" principle
  already applied to `POST /auto-trading/disable`.
- **`GET /auth/sessions`** — lists the caller's currently-active sessions
  (not revoked, not expired), finally surfacing `device_info`. This is
  the first code in the repository that ever reads that column back.
- **`POST /auth/sessions/{id}/revoke`** — revokes a specific session by
  id, ownership-checked the same way `/paper/*` and `/replay/*` sessions
  are: a non-owner gets `404`, never a `403` that would confirm the
  session exists at all.

`tests/api/test_auth_sessions.py` proves: logging out actually invalidates
the refresh token (a subsequent `/auth/refresh` with the same token
returns `401`); logout is idempotent for an already-revoked session and
tolerates a garbage token without raising; `GET /auth/sessions` reflects
`device_info` and shrinks once a listed session is revoked; a non-owner
revoking someone else's session gets `404` and the session stays
un-revoked. Reverting `logout()`'s body to a no-op reproduces the original
gap immediately — the refresh token keeps working after "logout" (verified
by hand before committing).

## `Order.broker_account_id` was declared but never populated (§50, §53)

`Order` (`app/database/models/trading.py`) has carried a nullable
`broker_account_id` foreign key to `broker_accounts` from early on — the
column that exists specifically so a placed order can be traced back to
*which* of a user's connected broker accounts actually executed it (a user
can, over time, connect and disconnect several). Nothing in the codebase
ever wrote to it. `grep -rn "broker_account_id" app/` found exactly one
hit before this fix: the column's own declaration. Every order ever
persisted — live, paper, or otherwise — had `broker_account_id = NULL`,
silently. This matters the moment a user reconnects a broker under a new
`BrokerAccount` row (e.g. after a disconnect/reconnect, or switching from
Upstox to Dhan): without this column, there is no way to answer "which
account placed this specific historical order" from the `orders` table
alone, which blueprint §50/§53's broker-abstraction design assumes is
possible (it's exactly the kind of per-account audit trail a real trading
system needs before it can be trusted with real money).

The root cause was `app/trading/broker_resolver.py`'s `resolve_broker()`:
it already looked up the user's active `BrokerAccount` row to decide which
adapter to build (`MockBroker`, `UpstoxBroker`, or `DhanBroker`), but
returned only the adapter — `account.id` was read, used to decrypt
credentials, and then discarded once the adapter was constructed. Nothing
downstream ever had the id to pass along, so `Order.broker_account_id`
couldn't have been populated by any amount of application-layer wiring
without touching this function first.

Fixed by changing `resolve_broker`'s return type from `Broker` to
`tuple[Broker, uuid.UUID | None]` — `None` specifically for the no-account
case (`MockBroker` with nothing connected, Stage 9's honest default: there
is no account to attribute a paper order to), and the resolved account's
id in every other case, including the `BrokerName.PAPER` "connected but
explicitly paper" case, where a real `BrokerAccount` row exists even
though it still trades against `MockBroker`. The id then threads through:
`app/api/orders.py`'s `_UserTradingStack` now stores `broker_account_id`
alongside its `broker` (set once, at stack creation, in `_stack_for`), and
both `persist_order(...)` call sites (`place_order`, `cancel_order`) pass
`stack.broker_account_id` through to `app/trading/persistence.py`'s
`persist_order()`, which now accepts a `broker_account_id` parameter
(`None` by default, since backtest/replay/paper-session code paths that
call `persist_order` never had a `BrokerAccount` to resolve in the first
place) and writes it onto the `OrderRow` at creation.

`tests/trading/test_broker_resolver.py`'s existing four resolution-path
tests now assert on the returned id as well as the adapter type (`None`
for no-account and disconnected-account cases; the connected account's own
id for active Upstox/Dhan accounts). `tests/api/test_orders.py` adds
`test_place_order_records_which_broker_account_executed_it`, which
connects an ACTIVE `PAPER` `BrokerAccount` (chosen specifically so the
test exercises the real `POST /orders` HTTP path without needing live
Upstox/Dhan credentials — `PAPER` still resolves to `MockBroker`, but,
unlike the no-account case, does so with a real account id to check
against) and asserts the persisted `Order.broker_account_id` matches it
end-to-end. Reverting the `OrderRow(...)` construction to drop
`broker_account_id` reproduces the original bug immediately — this new
test fails with a `NULL` mismatch — confirmed by hand before restoring
the fix.

## The daily/weekly loss limit never actually stopped live trading (§56-57)

`_UserTradingStack` (`app/api/orders.py`), the per-user in-memory
order/position/risk bundle behind the manual/live `POST /orders` path,
initializes `daily_pnl` and `weekly_pnl` to `0.0` alongside `trades_today`.
`trades_today` is correctly incremented on every new order. `daily_pnl`
and `weekly_pnl` were not — nothing in the file ever assigned to them
again. `place_order` already computes exactly the number needed:
`realized_delta = position_after.realized_pnl - realized_pnl_before`,
used purely to decide whether to journal a `Trade` row, then discarded.

The consequence: `RiskEngine.evaluate` (`app/risk/engine.py`) computes
`daily_loss_pct = max(-proposal.daily_pnl, 0) / proposal.account_balance
* 100` and rejects the trade when that exceeds `max_daily_loss_pct`
(default 2%) — the account's core stop-trading safety control. Since
`proposal.daily_pnl` was always `0.0` for every call from `POST /orders`,
`daily_loss_pct` was always `0`, which always passed. The same held for
`weekly_loss_limit`. However badly a user's live-connected account lost
money in a day, the one path that talks to a real broker would never stop
placing new orders because of it — the exact opposite of what a "daily
loss limit" is for.

This is a straightforward gap rather than a design choice:
`PaperTradingEngine` (`app/paper/engine.py`), which backs `/paper/*` and
autonomous trading, already does this correctly in `_maybe_exit`:
```python
pnl = position.realized_pnl - realized_before
self.daily_pnl += pnl
self.weekly_pnl += pnl
```
`app/api/orders.py`'s `place_order` never had the equivalent two lines.
It went unnoticed because the existing daily-loss-limit test
(`test_place_order_rejected_for_daily_loss_limit_notifies_distinctly`)
monkeypatches `RiskEngine.evaluate` directly to force a rejection, so it
exercises the notification-dispatch logic built on top of this bookkeeping
without ever exercising the bookkeeping itself.

Fixed by accumulating `realized_delta` into `stack.daily_pnl` and
`stack.weekly_pnl` in `place_order`, right where it already computes
`realized_delta` for the trade-journal decision — mirroring
`PaperTradingEngine._maybe_exit` exactly. New test
`test_daily_loss_limit_is_enforced_after_a_real_realized_loss` in
`tests/api/test_orders.py` places a real loss (no monkeypatching) — open
a long position, close it at a price far enough away to realize a loss
past the 2% default limit — and confirms a subsequent order is genuinely
rejected with a `DAILY_LOSS_LIMIT` notification. Reverting the two-line
fix reproduces the original bug immediately: this new test fails because
the third order is approved instead of rejected — confirmed by hand
before restoring the fix.

Note this fix does not add calendar-boundary resets for `daily_pnl`/
`weekly_pnl` — neither `_UserTradingStack` nor `PaperTradingEngine` reset
these counters at day/week boundaries today, since both are in-memory
structures whose lifetime in the current single-process deployment
roughly matches a trading session. A production deployment that keeps a
process alive across midnight would need that reset logic added to both
places identically; tracked as a follow-up, not folded into this fix so
as to keep it scoped to restoring parity between the two paths.

## "Daily"/"weekly" risk limits never reset — they were lifetime-of-process limits (§56-57)

This is the direct follow-up flagged (and deliberately deferred) by the
previous fix above: making `daily_pnl`/`weekly_pnl` actually accumulate
was necessary but not sufficient. Neither `_UserTradingStack`
(`app/api/orders.py`) nor `PaperTradingEngine` (`app/paper/engine.py`)
ever reset `trades_today`/`daily_pnl`/`weekly_pnl` at a day or week
boundary — both are long-lived in-memory objects (`_STACKS` in
`app/api/orders.py`, `AutoTradeSupervisor._engines` in
`app/workers/auto_trade_worker.py`) that persist for the life of the API
process or worker, so in practice these counters only ever cleared on a
restart.

The consequence: `max_trades_per_day` (`app/risk/engine.py`, default 10)
checks `proposal.trades_today < limits.max_trades_per_day` — once a
user's *cumulative* order count since the process last restarted hits 10,
every order is rejected from then on, not just for the rest of that
trading day. `daily_loss_limit`/`weekly_loss_limit` have the mirror
problem: a bad trading day early in a long-running process's uptime keeps
suppressing new trades on every later day too, since `daily_pnl` never
zeroes out to start the next day's evaluation from a clean baseline — the
opposite of what a "daily" limit is supposed to do. A production
deployment (a long-lived process is the norm, not periodic per-day
restarts) would have these limits silently stop being daily/weekly and
start being "since we last deployed."

Fixed by giving both classes a `_roll_risk_window(now)` method that
tracks the current UTC calendar day and ISO week as bucket keys (`(now.date()`
and `now.isocalendar()[:2]` respectively — the same bucketing
`app.smc.liquidity.detect_session_levels` already uses for day/week
liquidity levels) and resets `trades_today`/`daily_pnl` when the day key
changes, and `weekly_pnl` when the week key changes:
- `_UserTradingStack._roll_risk_window` is called at the top of
  `place_order` with `datetime.now(timezone.utc)` — the manual/live order
  path has no other clock to use.
- `PaperTradingEngine._roll_risk_window` is called at the top of
  `on_candle` with `candle.timestamp` instead — this engine already
  treats candle time as its logical clock everywhere else, and doing the
  same here means a backtest-speed replay of paper trading doesn't roll
  its risk window on wall-clock ticks that have nothing to do with the
  simulated market time.

Both methods guard on the stored bucket key being `None` (a freshly
constructed stack/engine has no "today" to compare against yet) so the
very first call establishes the window without spuriously zeroing
counters that were never non-zero anyway.

`tests/api/test_orders.py::test_user_trading_stack_resets_daily_and_weekly_counters_at_boundaries`
and `tests/paper/test_engine.py::test_paper_engine_resets_daily_and_weekly_counters_at_boundaries`
drive each `_roll_risk_window` directly across a same-day call (nothing
resets), a next-day call (daily counters reset, weekly does not, since
the fixture week starts on a Monday), and a next-week call (weekly resets
too). Reverting either method to a no-op reproduces the original bug
immediately — both new tests fail because the counters never zero out —
confirmed by hand before restoring the fix.

## The "repeated order rejection" circuit breaker never actually tripped (§57)

Blueprint §57's system-level risk controls list five circuit breakers:
market-data timeout, broker disconnect, unexpected price jump, repeated
order rejection, and system error. `RiskEngine.evaluate`
(`app/risk/engine.py`) already implements `no_repeated_rejections` as a
real check —
```python
checks.append(
    RiskCheck("no_repeated_rejections", proposal.repeated_rejections < limits.max_repeated_rejections)
)
```
with a real threshold (`RiskLimits.max_repeated_rejections = 3`). But
`TradeRiskProposal.repeated_rejections` defaults to `0`, and neither call
site that constructs a `TradeRiskProposal` — `app/api/orders.py`'s
`place_order` or `app/paper/engine.py`'s `on_candle` — ever set it to
anything else. Since `0 < 3` is always true, this check could never fail,
for live or paper trading, no matter how many times an account's orders
in a row got rejected by the broker.

This is a different flavor of "dead risk-check input" than the
daily/weekly loss limit fixed earlier: `repeated_rejections` isn't about
*risk-engine* rejections (a pre-trade `REJECT` decision never reaches the
broker at all, and is already covered by every other check in this
function) — it's meant to catch *broker-level* rejections (insufficient
margin, a bad symbol, a stale/invalid session token) that happen after
risk approval, inside `app/trading/execution.py`'s `ExecutionEngine.submit`,
which sets `OrderStatus.REJECTED` when `broker.place_order()` comes back
rejected. A run of these in a row is exactly the kind of "something is
systematically wrong" signal blueprint §57 wants to trip a breaker on.

Fixed by adding a `repeated_rejections` counter to both `_UserTradingStack`
(`app/api/orders.py`) and `PaperTradingEngine` (`app/paper/engine.py`),
mirroring the existing `trades_today`/`daily_pnl` pattern: read into the
`TradeRiskProposal` before each attempt, then updated after a *freshly
created* order (not a duplicate idempotent replay) finishes execution —
incremented when the final status is `REJECTED`, reset to `0` on any
order that actually executes. Resetting on success (rather than requiring
manual intervention, unlike `KillSwitchState`) means a transient issue —
a broker outage that later clears — doesn't permanently lock an account
out once it's resolved.

`tests/api/test_orders.py::test_repeated_broker_rejections_trip_the_circuit_breaker`
uses `MockBroker.reject_probability = 1.0` (deliberately built into
`MockBroker` for exactly this kind of deterministic test) to force 3
consecutive real broker-level rejections through the actual `POST /orders`
path, then confirms a 4th attempt is blocked by `no_repeated_rejections`
before ever reaching the broker. `tests/paper/test_engine.py` adds two
tests: `test_paper_engine_records_broker_rejections` proves a real broker
rejection is reflected in the counter at all (the write side), and
`test_paper_engine_enforces_repeated_rejections_limit` proves a
pre-tripped counter actually blocks the next signal (the read side).
Reverting either the read-side wiring (the `repeated_rejections=` proposal
argument) or the write-side update reproduces the original bug
immediately in the corresponding tests — confirmed by hand, both ways,
before restoring the fix.

This PR left one sibling gap open — `no_abnormal_price_jump` had the
identical shape of bug (see the next section) — since it needed new
rolling-price infrastructure that didn't exist yet, rather than just
wiring up data available elsewhere. It's fixed below.

## The "unexpected price jump" circuit breaker never actually tripped (§57)

The last of blueprint §57's five system-level circuit breakers to still
be dead code, after the two above: `RiskEngine.evaluate` (`app/risk/engine.py`)
already implements `no_abnormal_price_jump` as a real check —
```python
checks.append(
    RiskCheck("no_abnormal_price_jump", proposal.recent_price_jump_pct <= limits.max_price_jump_pct)
)
```
against a real threshold (`RiskLimits.max_price_jump_pct = 5.0`), but
`TradeRiskProposal.recent_price_jump_pct` defaults to `0.0`, and neither
`app/api/orders.py`'s `place_order` nor `app/paper/engine.py`'s
`on_candle` ever set it. `0.0 <= 5.0` is always true, so an account could
never be blocked from opening a new position during a real flash-crash or
price spike — exactly the scenario this check exists for.

Unlike `repeated_rejections`, this one genuinely had no data to wire up:
`app/core/redis.py`'s price cache (`set_latest_price`/`get_latest_price`/
`get_price_age_seconds`) only ever stored the current tick — there was no
previous tick anywhere to diff against, live or paper.

Fixed with the smallest infrastructure addition that closes the gap:
- `set_latest_price` now stashes whatever was latest a moment ago under a
  new `price_prev:{symbol}` key (same TTL as the existing keys, so it
  expires and needs no separate cleanup) *before* overwriting it with the
  new tick. `app/workers/market_data_worker.py`'s `process_tick` is the
  only writer, one tick at a time on one sequential loop, so a plain read
  before the write pipeline can't race with a concurrent writer for the
  same symbol.
- A new `get_price_jump_pct(symbol)` reads both keys and returns
  `abs(latest - previous) / previous * 100`, or `None` when there's
  nothing to compare against yet — the same "no data yet, not an infinite
  jump" convention `get_price_age_seconds` already established.
- `app/api/orders.py`'s `place_order` wires this in exactly like
  `market_data_age_seconds` two lines above it:
  `recent_price_jump_pct=await get_price_jump_pct(payload.symbol) or 0.0`.
- `PaperTradingEngine` needed no Redis at all: it already keeps its own
  `self.candles` history, so `recent_price_jump_pct` there is just the
  percent move between `self.candles[-1]` (the current candle, already
  appended by the time the proposal is built) and `self.candles[-2]` —
  always safe to index, since `on_candle` already returns early when
  fewer than 3 candles exist.

`tests/test_core_redis.py` adds two tests for the new Redis primitive:
`None` with only one tick ever set, and the correct percentage — computed
against the *immediately preceding* tick, not the first one ever seen —
across a sequence of three. `tests/api/test_orders.py::test_abnormal_price_jump_blocks_a_new_order`
drives two real ticks 10% apart through `set_latest_price` and confirms
`POST /orders` genuinely rejects the next order with
`no_abnormal_price_jump`. `tests/paper/test_engine.py::test_paper_engine_enforces_abnormal_price_jump_limit`
uses the SETUP fixture's own real, unmodified entry-candle price move
(~1.87%, the candle the fixture's strategy actually matches and places an
order on) and simply lowers `max_price_jump_pct` below it, rather than
synthesizing an artificial spike. Reverting either call site's wiring
reproduces the original bug immediately in all three tests — confirmed by
hand, before restoring the fix.

This closes out every known instance of the "risk-check input never
actually set" pattern found across daily/weekly P&L, repeated rejections,
and now price jumps — all three of §56-57's account/system-level checks
that had a real implementation and a real threshold, but no live data
feeding them.

## A forged `entry` could launder an oversized live order past every notional risk check (§56-57)

Every risk-check input fixed in the last three rounds (`daily_pnl`,
`repeated_rejections`, `recent_price_jump_pct`) was a case of a *missing*
value — a field nobody set. This one is different: `POST /orders`'s
`entry`/`stop` are always set, by the client, with zero validation
(`PlaceOrderRequest`, `app/api/orders.py`) — and that trust turns the risk
engine's own math into a way around it.

`calculate_position_size` (`app/risk/engine.py`) sizes a position purely
from the client-supplied gap between `entry` and `stop`:
```python
risk_per_unit = abs(entry - stop)
risk_amount = account_balance * (risk_percent / 100)
quantity = risk_amount / risk_per_unit
```
Every notional-based risk check in the same `evaluate()` call —
`exposure_limit`, `strategy_allocation_limit`, `correlated_exposure_limit`
— then computes that trade's notional as `quantity * proposal.entry`,
using the *same* client-supplied `entry`. Substituting the formula above:
```
notional = (risk_amount / |entry - stop|) * entry
```
A client can make `|entry - stop|` arbitrarily small (e.g. `entry=150.0,
stop=149.9`) to make `quantity` arbitrarily large, while `notional`
(computed from that same tiny gap and that same `entry`) stays whatever
size the client wants it to look like — small enough to clear every
exposure check. For `MockBroker`, this is harmless: `place_order` already
seeds `MockBroker`'s quote from `payload.entry` before evaluating risk
(`if isinstance(stack.broker, MockBroker): stack.broker.set_quote(...)`,
with the comment "never let a real order's fill price be dictated by the
caller" already flagging that this is deliberately mock-only behavior),
so the mock fill happens at the same fabricated price the risk math used.
But a real broker (Upstox/Dhan) fills a MARKET order at its own,
completely independent real market price — so the *quantity* computed
from the forged `entry`/`stop` gap is what actually gets submitted
(`ExecutionEngine.submit`, `OrderRequest(quantity=order.quantity, ...)`),
executed at a real price that has nothing to do with the notional the
risk engine just approved.

Fixed by adding a new `RiskEngine.evaluate` check, `entry_matches_market`,
against a new `TradeRiskProposal.entry_deviation_pct` field and
`RiskLimits.max_entry_deviation_pct` (default 1.0%). `place_order` now
fetches the broker's own quote right after the existing `MockBroker`
seeding step —
```python
quote = await stack.broker.get_quote(payload.symbol)
entry_deviation_pct = abs(payload.entry - quote.ltp) / quote.ltp * 100 if quote.ltp else 0.0
```
— so for `MockBroker` this is always `0%` (the quote was just set *from*
`payload.entry`, by the same existing seeding step), leaving paper trading
completely unaffected, while for a real broker it reflects a genuine gap
between the claimed and real price. `PaperTradingEngine` doesn't need
this at all: its `entry` comes from the strategy engine's own analysis of
real candle data, never from arbitrary client input, so this specific
exploit doesn't apply there.

`tests/risk/test_engine.py` adds a unit test proving `entry_deviation_pct`
defaults to a no-op (mirroring the pattern already established for
`correlated_exposure`) and one proving a 5% deviation is rejected against
the 1% default limit. `tests/api/test_orders.py` adds a `_FakeRealBroker`
test double — a minimal `Broker` implementation whose quote is fixed and
independent of client input, standing in for a real connected broker
since `MockBroker` cannot exercise this scenario by design — and
`test_entry_far_from_the_real_broker_quote_is_rejected`, which swaps a
live stack's broker to it mid-test and confirms `POST /orders` genuinely
rejects an order whose claimed `entry` is far from that fixed quote.
Reverting either the new check or its wiring in `place_order` reproduces
the original bug immediately in all three tests — confirmed by hand,
both independently, before restoring the fix.

## A live order's `stop` was never attached to its position (§60)

`PlaceOrderRequest.stop` (`app/api/orders.py`) is required on every
`POST /orders` call and already does real work — `calculate_position_size`
sizes the order from `|entry - stop|`, and the risk engine's
`valid_stop_distance`/notional checks read it too. But after that, nothing
in `place_order` ever wrote it onto the resulting `PositionRecord.stop`.
Compare `PaperTradingEngine._maybe_enter`-equivalent code path in
`app/paper/engine.py` (`if created: new_position.stop = result.stop`,
right after opening a paper/auto-trade entry) — the manual/live path is
the one place in the codebase that took a stop as input and then dropped
it before it reached the position it was supposed to protect.

The consequence compounds: `positions.stop` (the DB column,
`app/trading/persistence.py`) faithfully mirrors whatever's in the
in-memory `PositionRecord` — which was always `None` for a live position,
so the column was dead-on-arrival for every `ExecutionMode.LIVE` row even
though the schema and persistence layer both already worked correctly for
paper/backtest rows. A user placing a live order via this endpoint had no
way to even confirm a stop was recorded, since `GET /positions` (and
everything downstream of the DB row) had nothing to show.

Fixed by setting `position_after.stop = payload.stop` in `place_order`,
but only when this fill actually opened, added to, or flipped into a
position in `payload.direction`:
```python
if position_after.is_open and position_after.is_long == (payload.direction == Direction.LONG):
    position_after.stop = payload.stop
```
This condition matters: `POST /orders` (unlike `PaperTradingEngine`,
which only ever evaluates a fresh entry while flat) is also how a
position gets closed or reduced — a SHORT fill against an open LONG
position, say. A closing/reducing fill's own `stop` has nothing to do
with the *original* entry's protective level, so the condition above
skips it whenever the position's final direction doesn't match the fill
that was just placed; it still fires correctly on the flip case (a fill
big enough to close the old side and open a fresh one in the new
direction), since the resulting position's direction then does match.

`tests/api/test_orders.py::test_live_order_records_stop_on_the_resulting_position`
covers all three shapes: opening records the stop, adding to the same
side updates it, and a smaller reducing fill in the opposite direction
leaves it untouched. Reverting the fix reproduces the original bug
immediately — the first assertion alone fails, since the DB row's `stop`
is `None` — confirmed by hand before restoring it.

**Known follow-up, deliberately not fixed here:** recording the stop is
necessary but not sufficient for it to do anything. Nothing yet
*enforces* a live position's stop — there is no equivalent of
`PaperTradingEngine._maybe_exit`'s candle-driven stop/target check running
against live positions, and `trigger_price` (present on `Order`,
`OrderRequest`, and already forwarded by `UpstoxBroker.place_order`) is
never threaded through `OrderManager.create_order`/`ExecutionEngine.submit`,
so even an `order_type=SL`/`SL_M` order sends the broker a trigger price
of `0`, not the user's stop. Actually enforcing a live stop needs either a
monitoring worker (polling open live positions' `.stop` against fresh
quotes and submitting a closing order, analogous to `_maybe_exit`) or
wiring `trigger_price` end-to-end so the broker holds the protective order
natively — both larger, separate pieces of work than restoring the value
this fix closes the gap on.

## A forged options leg `premium` could launder an oversized order the same way `entry` could (§56-57)

The same vulnerability as "A forged `entry` could launder an oversized
live order past every notional risk check" above, reopened in a
different endpoint that fix never touched: `POST /options/execute`.

`ExecuteOptionLegRequest.premium` is fully client-controlled and feeds
straight into `compute_payoff_summary` — `payoff.max_loss`/
`capital_requirement`, which `evaluate_options_risk`'s `exposure_limit`
check reads directly. Unlike `POST /orders`, an options leg's `quantity`
isn't derived from any risk-sized formula either — it's independent
client input — so a client could submit a large `quantity` and a
premium far from reality (small for a long leg, large for a short one)
and still show a small `max_loss`, clearing `exposure_limit`. On
submission, a real broker fills the `MARKET` order at its own price,
completely disconnected from the claimed `premium` — only `MockBroker`
is seeded from it (`stack.broker.set_quote(leg.symbol, ltp=leg.premium)`),
same as the `POST /orders` case.

What made this one easy to miss: `execute_options_strategy` already
fetches each leg's real `OptionSnapshot` (bid/ask) for the liquidity
check — the data needed to catch this was already being pulled from the
database on every call, then discarded once `evaluate_liquidity` was
done with it, never compared against the claimed `premium`.

Fixed the same way as the `entry` case: a new `OptionsRiskProposal.premium_deviation_pct`
field (default `0.0`) and a `premium_matches_market` check in
`evaluate_options_risk`, gated by a new `RiskLimits.max_premium_deviation_pct`
(default `5.0%` — wider than `max_entry_deviation_pct`'s `1.0%`, since
option premiums genuinely move more between quotes than an equity's last
traded price). `execute_options_strategy`'s existing per-leg liquidity
loop now also computes `abs(leg.premium - mid) / mid * 100` against each
snapshot's bid/ask mid when both are present, tracking the *worst*
deviation across all legs — one leg with a wildly wrong premium is
reason enough to reject the whole strategy. When no leg has snapshot
data (still the common case — no options-chain ingestion pipeline exists
in this environment), the deviation stays `0.0`, a no-op, exactly
mirroring the existing "warn, don't reject" treatment liquidity already
gets for missing data.

`tests/risk/test_options_risk.py` adds unit tests mirroring the `entry`
case's: default no-op, and a 10% deviation rejected against the 5%
default. `tests/api/test_options_execute.py::test_execute_rejects_when_premium_deviates_from_real_quote`
seeds a real `OptionChainSnapshot`/`OptionContract`/`OptionSnapshot` with
a known bid/ask and confirms `POST /options/execute` genuinely rejects a
claimed premium far from that mid. Reverting either the check or its
wiring in `execute_options_strategy` reproduces the original bug
immediately in all three tests — confirmed by hand, both independently,
before restoring the fix.

## Worker heartbeats flapped healthy/DOWN on perfectly healthy workers (§117)

`app/core/redis.py`'s worker-heartbeat mechanism (blueprint §117
"Workers 🟢") is a TTL'd Redis key each worker's loop refreshes on every
pass — `GET /health`/`GET /admin/system-health` treat a missing key as
that worker being down. The TTL (`_HEARTBEAT_TTL_SECONDS`) was `30`
seconds. But every registered worker's `run()` loop —
`ScannerWorker`/`AutoTradeSupervisor` (`app/workers/scanner_worker.py`,
`app/workers/auto_trade_worker.py`, both constructed with
`interval_seconds=60.0` in `app/workers/main.py`) and the live
reconciliation loop (`app/trading/live_reconciliation.py`, `run()`
defaulting to `interval_seconds=60.0` with no override anywhere) — calls
`heartbeat()` exactly once per 60-second cycle: `run_once()`/reconcile,
then `heartbeat(name)`, then `sleep(interval_seconds)`.

30 seconds is shorter than 60 seconds. So the key expired roughly halfway
through every single cycle, for every one of these three workers — not
because anything was stuck or crashed, but because the TTL was simply
set wrong relative to how often it actually gets refreshed. `worker_is_alive()`
sawtoothed `True`→`False`→`True` forever on workers running exactly on
schedule, making `GET /health` indistinguishable between "this worker
crashed ten minutes ago" and "this worker is fine, it just refreshed 31
seconds ago." Anything wired to this for alerting or a readiness probe
would either page on every single cycle (unusable) or get tuned down
until it stopped catching real crashes too (the standard alert-fatigue
failure mode) — the exact opposite of what a liveness signal is for.
`market_data`'s heartbeat wasn't affected — it refreshes per-tick, far
more often than every 30 seconds, so its key never had the chance to
expire between refreshes.

Fixed by raising `_HEARTBEAT_TTL_SECONDS` to `90` — comfortably longer
than the known 60-second interval, with margin for one slow pass, while
still going stale (and reporting `DOWN`) within roughly one and a half
cycles of an actually stuck or crashed loop. All three affected workers'
intervals are hardcoded literals, not environment-configurable, so one
shared constant is sufficient here — there's no scenario in this codebase
today where a worker's own interval could outgrow this TTL without a code
change alongside it.

`tests/test_core_redis.py::test_heartbeat_ttl_exceeds_the_longest_worker_loop_interval`
checks the actual Redis `TTL` on a freshly-set heartbeat key against the
known 60-second interval, rather than sleeping through a real cycle in a
test (which would either be too slow or too flaky to be worth writing).
Reverting the constant back to `30` reproduces the original bug
immediately — this test fails with `assert 30 > 60.0` — confirmed by hand
before restoring the fix.

## The `Order.broker_account_id` fix never reached its sibling endpoint (§50, §53)

The bug documented above in "`Order.broker_account_id` was declared but
never populated" was fixed by threading `stack.broker_account_id`
through `app/api/orders.py`'s two `persist_order` call sites
(`place_order`, `cancel_order`). `app/api/options.py`'s
`execute_options_strategy` was never touched by that fix — and its own
docstring says it places real orders "through the same broker/risk/
persistence pipeline `POST /orders` uses." It builds the exact same
`_UserTradingStack` via `_stack_for(user, db)`, which already carries
`stack.broker_account_id`, but its `persist_order(...)` call
(`app/api/options.py`) never passed it along:
```python
final_order = stack.order_manager.get(order.id)
await persist_order(db, final_order, user.id, instrument.id, execution_mode=execution_mode)
```
Every options leg ever executed through this endpoint — including
through a real, connected Upstox/Dhan account — was persisted with
`Order.broker_account_id` permanently `NULL`, the identical gap the
earlier fix closed for `POST /orders`, just left open in this sibling
endpoint that shares the same stack and the same persistence call.

Fixed by adding `broker_account_id=stack.broker_account_id` to this call,
mirroring `place_order`'s pattern exactly. New
`tests/api/test_options_execute.py::test_execute_records_which_broker_account_executed_it`
mirrors `test_orders.py`'s existing test for the same fix: connects an
ACTIVE `PAPER` `BrokerAccount` (resolves to `MockBroker`, so no real
broker credentials are needed), executes a two-leg strategy, and asserts
both persisted `Order` rows' `broker_account_id` match it. Reverting the
one-line fix reproduces the original bug immediately — confirmed by hand
before restoring it.

## Options risk decisions left no audit trail and rejections never notified (§63)

A second sibling-endpoint gap in `app/api/options.py`'s `execute_options_strategy`,
found the same way as the `broker_account_id` one above: it evaluates
`evaluate_options_risk(...)` but, until this fix, did nothing with the
result besides checking `decision.approved` —
```python
decision = evaluate_options_risk(risk_proposal, limits=stack.risk_engine.limits)
if not decision.approved:
    raise HTTPException(status.HTTP_403_FORBIDDEN, f"Risk engine rejected this strategy: {decision.reason}")
```
Compare `POST /orders`'s `place_order` (`app/api/orders.py`), which for
the identical kind of event always persists a `RiskEvent` row — approved
*or* rejected — before checking `decision.approved`, and on rejection
also fires an `ORDER_REJECTED` notification before raising. That fix
("Live order rejections were the one path that never notified", earlier
in this document) predates this endpoint's `/execute` action having this
shape; two *later* rounds patched `execute_options_strategy` for other
`orders.py`-parity gaps (the forged-premium check, `broker_account_id`)
but both missed this one.

Concretely: a multi-leg options strategy rejected for excessive
projected exposure, unacceptable liquidity, a forged premium, stale
market data, or an unhealthy broker got only the one-shot 403 response.
`GET /notifications` never showed it. No `RiskEvent` audit row existed to
reconstruct what happened — and unlike every other trading path in this
codebase (paper, auto-trade, manual orders), there was no persistent
record that a risk decision had been evaluated for this account's
options activity at all, approvals included.

Fixed by mirroring `place_order`'s pattern exactly: an unconditional
`RiskEvent(user_id, decision, reason, checks)` write right after
`evaluate_options_risk` returns, and — only on rejection — a
`create_notification(..., NotificationType.ORDER_REJECTED, ...)` call
before the `HTTPException`. `evaluate_options_risk` has no
`daily_loss_limit`-equivalent check the way `RiskEngine.evaluate` does,
so no ternary is needed here — `ORDER_REJECTED` is the only notification
type this path can produce.

New `tests/api/test_options_execute.py::test_execute_risk_rejection_writes_audit_row_and_notifies`
drives the same oversized spread the existing exposure-limit test already
uses and asserts both a `RiskEvent` row (`decision == REJECT`) and a
`Notification` row (`type == ORDER_REJECTED`) exist afterward. Reverting
the fix reproduces the original bug immediately — this test fails with
zero rows in both tables — confirmed by hand before restoring it.

## Paper trading and autonomous trading never wrote a `RiskEvent` audit row (§63)

`GET /admin/risk-events` (`app/api/admin.py`) is the only queryable audit
trail of what `RiskEngine.evaluate()` actually decided — the raw
`{check_name: passed}` map, not a free-text summary. `POST /orders`
(`app/api/orders.py`) and `POST /options/execute` (`app/api/options.py`,
the two fixes directly above this one) both write a `RiskEvent` row on
*every* decision, approved or rejected, right after calling their risk
engine. `app/paper/engine.py`'s `PaperTradingEngine.on_candle` — driven
identically by a manual paper session (`app/api/paper.py`'s `feed_candle`)
and by `AutoTradeSupervisor` (`app/workers/auto_trade_worker.py`, which
runs unattended, 24/7, across every eligible user/strategy/instrument) —
calls the exact same `RiskEngine.evaluate()`, but until this fix the
resulting `RiskDecisionResult` never left the engine as anything more than
two free-text strings (`risk_rejected_reason`, `risk_failed_check`) used
only to pick a notification type. No `RiskEvent` row was ever written from
either caller, for either outcome.

This was the same "sibling path missed a fix" shape as the two `options.py`
gaps above it, just one hop further out: `orders.py` and `options.py` are
both synchronous HTTP endpoints with a response the caller sees immediately
(a 403 with `decision.reason`, at minimum) even before either of those
`RiskEvent` fixes existed. Paper trading and autonomous trading have no such
synchronous observer — `AutoTradeSupervisor` in particular places or
rejects trades on a background timer with nobody watching the return value.
For a platform whose flagship feature is unattended autonomous trading,
this was exactly the audit trail an admin or compliance reviewer would need
to answer "why did the system approve or reject this trade at 3am", and it
was silently absent for every paper session and every autonomous trade ever
run — while the identical decision on a live order or an options strategy
was fully audited.

Fixed by widening `PaperTradeOutcome` (`app/paper/engine.py`) with a new
`risk_checks: dict[str, bool] | None` field, set from
`{c.name: c.passed for c in decision.checks}` on *both* branches of
`on_candle` — the early return when `decision.approved` is `False`, and the
final return after an order is created — so a caller can tell a real
decision was made (`risk_checks is not None`) even when it approved
everything with `PaperTradeOutcome` otherwise looking identical to the "no
signal matched" case. `app/api/paper.py`'s `feed_candle` and
`app/workers/auto_trade_worker.py`'s `_process` both now check
`outcome.risk_checks is not None` immediately after calling
`engine.on_candle(...)` and write a `RiskEvent(user_id, decision, reason,
checks)` row — `REJECT` when `outcome.risk_rejected_reason` is set,
`APPROVE` otherwise — mirroring `place_order`'s pattern exactly, before any
of the existing notification/audit-log branches run.

New tests: `tests/api/test_paper.py::test_paper_trading_writes_risk_event_audit_row_on_approval`
and `::test_paper_trading_writes_risk_event_audit_row_on_rejection` (the
latter reuses the existing `RiskEngine.evaluate` monkeypatch technique to
force a rejection deterministically), plus
`tests/workers/test_auto_trade_worker.py::test_supervisor_writes_risk_event_audit_row_for_the_opened_trade`.
Verified each fails against the pre-fix code (an assertion on
`len(risk_events) >= 1` with zero rows returned) and passes again once the
fix is restored.

## A closing trade's `exit_price` recorded the client's claimed price, not the broker's real fill (§61)

`POST /orders` and `POST /options/execute` both write a `trades` journal
row (`app/trading/persistence.py`'s `record_trade`, blueprint §61) whenever
a fill closes or reduces an existing position. On that row, `pnl` is
computed correctly — `position_after.realized_pnl - realized_pnl_before`,
where `PositionManager.apply_fill` derived the realized P&L from its
`price` argument, which is `final_order.average_fill_price`: the broker's
own reported fill (`ExecutionEngine.submit` sets
`order.average_fill_price = result.average_fill_price` straight from
`Broker.place_order`'s response). But `exit_price` on that same row was
set to `payload.entry` in `app/api/orders.py` and `leg.premium` in
`app/api/options.py` — the raw, client-supplied request field, never
`final_order.average_fill_price`, which sits right there on the same
`final_order` object `persist_order` already uses two lines earlier.

For any real broker (`UpstoxBroker`/`DhanBroker`), the actual MARKET-order
fill price is independent of whatever the client typed as `entry`/
`premium` — that is the entire premise behind the already-fixed
`entry_matches_market`/`premium_matches_market` checks earlier in this
document, whose ≤1%/≤5% deviation tolerance only bounds risk *sizing*, not
what gets permanently written to the trade journal as historical fact. So
even a fully legitimate, within-tolerance live order produced a `trades`
row where `exit_price` didn't match the price actually implied by its own
`pnl`/`entry_price` — an internally inconsistent, permanently-persisted
financial record, exactly the kind of discrepancy an audit or a SEBI
compliance review would flag.

This was invisible in every existing test because `MockBroker` fills a
MARKET order at exactly the quote both endpoints explicitly seed from the
client's own value right before submitting
(`stack.broker.set_quote(payload.symbol, ltp=payload.entry)` in
`orders.py`; the `leg.premium` equivalent in `options.py`), with the
default `slippage_pct=0.0` — so `average_fill_price` happened to equal
`payload.entry`/`leg.premium` by construction, for every prior test.
`app/api/paper.py`'s `feed_candle` already does this correctly
(`exit_price=candle.close`, the real simulated price), showing the
codebase already knew the right pattern — `orders.py`/`options.py` simply
didn't follow it for this one field.

Fixed by changing both `record_trade` calls to
`exit_price=final_order.average_fill_price` instead of the client-supplied
field. New tests:
`tests/api/test_orders.py::test_closing_trade_records_the_real_fill_price_not_the_claimed_entry`
and
`tests/api/test_options_execute.py::test_closing_leg_records_the_real_fill_price_not_the_claimed_premium`
— both reproduce a real broker whose fill price diverges from the client's
claim (`orders.py`'s test reuses `_FakeRealBroker`, a fixed-quote broker
double, syncing `stack.execution_engine.broker` too since `ExecutionEngine`
captures its own reference at construction; `options.py`'s test instead
mutates the existing `MockBroker.slippage_pct` in place, since that
broker's own fill logic is what needs to diverge from the quote it was
just seeded with) and assert the persisted `exit_price` matches the real
fill, not the claim. Verified both fail against the pre-fix code (asserting
the buggy client-supplied value) and pass once restored.

## WebSocket channels never detected a client disconnect (§64)

Every channel in `app/api/websockets.py` (`/ws/market`, `/ws/chart`,
`/ws/scanner`, `/ws/signals`, `/ws/orders`, `/ws/positions`, `/ws/replay`)
is a thin relay through the shared `_relay(websocket, channel)` helper.
Before this fix, `_relay` was:

```python
async def _relay(websocket, channel):
    await websocket.accept()
    try:
        async for message in subscribe(channel):
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()
```

This never calls `websocket.receive()`. In the ASGI websocket protocol, a
client disconnect — a clean close handshake *or* an abrupt drop (a killed
tab, a phone going to sleep, wifi loss) — is only delivered to the
application as a `{"type": "websocket.disconnect"}` message on the
**receive** side. `WebSocket.send()` only raises `WebSocketDisconnect`
after the *next* failed write; it can't proactively notice a client that's
already gone. `subscribe(channel)` (`app/core/redis.py`) blocks on Redis's
`pubsub.listen()`, so if nothing publishes to that channel after the
client disconnects, this coroutine simply parks forever: it never calls
`receive()` (so it can't learn about the disconnect that way) and never
calls `send()` again either (so the failed-write path never triggers). The
`application_state != CONNECTED` check was dead code for the same
reason — nothing else ever touches this websocket's `application_state`
once a real client is gone.

This is a real, unbounded resource leak, not a theoretical one: uvicorn's
`connection_lost` (fired when the transport actually dies) closes the
transport but never cancels the already-running ASGI application task, so
the leaked task and the Redis pub/sub subscription it holds open both
survive indefinitely. The per-user channels are the worst case — `/ws/orders`
and `/ws/positions` only publish when *that specific user* places an order
or their position changes, so a user who opens the dashboard and later
closes the tab without another order ever firing leaks a task and a Redis
subscription for the rest of the process's life. Ordinary client behavior
(switching tabs, a phone sleeping, a page reload) makes this an ongoing
leak over a trading day, eventually threatening Redis's `maxclients` limit
and the API process's own memory/FD limits — the same *class* of bug as
"A real Postgres connection leak, finally found" above, just in the
WebSocket/Redis-pubsub layer instead of the DB layer.

Fixed by running two tasks concurrently per connection: the existing
forward loop (`_forward`, unchanged logic) and a new `_watch_for_disconnect`
loop that calls `websocket.receive()` and returns as soon as it sees a
`websocket.disconnect` message (any other message type — clients aren't
expected to send anything on these one-way broadcast channels — is just
ignored). `_relay` now `asyncio.wait`s on both with
`return_when=FIRST_COMPLETED`, cancels whichever is still pending, and
propagates any real (non-disconnect) exception from whichever finished
first. Cancelling the forward task mid-`async for` while it's blocked
inside `subscribe(channel)`'s `pubsub.listen()` correctly triggers that
generator's own `finally` block (`pubsub.unsubscribe`/`pubsub.aclose`),
which is exactly the cleanup that was never reached before.

New `tests/api/test_websockets.py::test_relay_detects_disconnect_and_cleans_up_the_subscription`
calls `_relay` directly against a minimal fake `WebSocket` double whose
`receive()` resolves to a disconnect message after a short delay, with
nothing ever published to the channel — deliberately not using
`starlette.testclient.WebSocketTestSession` (`with client.websocket_connect(...):`),
whose own `__exit__` cancels the underlying ASGI app task unconditionally
via its anyio `TaskGroup` teardown, which would mask this exact bug
regardless of whether `_relay` itself ever learned about the disconnect.
Wrapped in `asyncio.wait_for(..., timeout=2.0)` and asserts the Redis
channel's `PUBSUB NUMSUB` count is `0` afterward. Verified the test times
out against the pre-fix code (proving the leak) and passes, completing
almost immediately with the subscription cleaned up, once the fix is
restored.

## Refresh-token reuse was never detected — a stolen token got a full rotation, silently (§69)

`app/auth/service.py`'s `refresh()` correctly implements token *rotation*:
every call revokes the presented session and issues a brand-new
session/refresh-token pair (`_issue_tokens`, minting a fresh, unrelated
`session_id`). But rotation is only half of the standard refresh-token
security model. The other half — reuse *detection* — didn't exist: an
already-rotated-out (or already-logged-out) refresh token being presented
again fell into the exact same generic branch as a garbage/forged token or
one that had simply expired:

```python
if (session is None or session.revoked or session.refresh_token_hash != hash_token(refresh_token)
        or session.expires_at < datetime.now(timezone.utc)):
    raise AuthError("Refresh token is no longer valid")
```

Presenting a refresh token that matches a session's stored hash *exactly*
but is already marked `revoked` is not an ordinary error — it's the
textbook signature of a stolen token: the legitimate client and a thief
raced to use the same token, and whichever lost gets exactly this error.
Before this fix, that signal was silently swallowed: no revocation of
whatever session the earlier rotation produced (nothing even links back to
it — `UserSession` has no lineage field at all), no `record_audit` call
(unlike `login()`, which logs `login.failed`, `refresh()` never called
`record_audit` on any branch, success or failure), and no notification.
Blueprint §69's whole justification for adding self-service session
management (`POST /auth/logout`, `POST /auth/sessions/{id}/revoke` — see
"No way to log out" earlier in this document) was that a stolen refresh
token "stayed valid until its multi-day natural expiry with no
self-service remediation" — but that remediation only helps if the
legitimate user notices something is wrong, and reuse detection is exactly
the mechanism that's supposed to surface that. Without it, a thief who
steals a refresh token (a leaked log, XSS, a compromised device) and wins
the race gets a fully valid, long-lived session running completely
undetected, with zero audit trail anywhere an admin or the user could ever
find it — for an account with live broker credentials and real money on
the line.

Fixed in `refresh()`: when the presented token's hash matches a session
that's already `revoked`, that's treated as reuse rather than an ordinary
invalid token. The response is the same containment a user hitting the
kill switch would get — every currently-active session for that user is
revoked in one `UPDATE` (not just the one this token names; the session
the original rotation produced isn't linked to it at all, so revoking
broadly is the only reliable way to cut off whichever side of the race
actually has live tokens) — plus a `record_audit(actor="system",
action="auth.refresh_token_reuse_detected", ...)` row, before raising the
same `AuthError` the caller already saw for any other invalid-token case
(no information leak about *why* it failed). A non-matching hash or a
missing session still falls through to the ordinary error path unchanged,
so a plain garbage/forged token never triggers this.

New `tests/api/test_auth_sessions.py::test_refresh_token_reuse_revokes_every_session_and_audits`
logs in, rotates once to get a second token, then replays the first
(already-rotated-out) token — asserting the reuse is rejected, that the
*second* token (produced by the legitimate rotation, with no link back to
the first) is also dead afterward, that every session row for the user is
`revoked`, and that matching `AuditLog` rows exist. Verified the test
fails against the pre-fix code and passes once the fix is restored.

## Paper/auto-trade `Trade.exit_price` also recorded the wrong price — on the SL/TP-exit path this time (§61)

**Correction to the section above:** its claim that `app/api/paper.py`'s
`feed_candle` "already does this correctly (`exit_price=candle.close`, the
real simulated price)" was wrong for the one path that matters most —
a stop-loss or take-profit exit — and this round found it.
`PaperTradingEngine._maybe_exit` (`app/paper/engine.py`) closes a position
when a candle's `high`/`low` crosses `position.stop`/`position.target`,
fills the closing order at *that trigger level* (`self.broker.set_quote(
self.symbol, ltp=trigger_price)`, then submits through the same
`ExecutionEngine`/`PositionManager.apply_fill` pipeline `POST /orders`
uses) — **not** at the candle's close. A stop-loss can trigger intraday
(`candle.low <= position.stop`) while the candle still closes well above
it; both `app/api/paper.py`'s `feed_candle` and
`app/workers/auto_trade_worker.py`'s `_process` nonetheless journaled the
closing `Trade` row with `exit_price=candle.close`/`latest.close` — a
value the position was never actually filled at — while `pnl` on that same
row was correctly derived from the real stop/target fill. The exact same
"internally inconsistent trade-journal row" defect as the section above,
just reached via the bracket-exit path instead of a live order fill, and
missed by that round because its own regression test
(`test_paper_trading_notifies_sl_hit_on_stop_loss_exit`) only ever
asserted `pnl < 0.0`, never checked `exit_price` against anything, and
`grep`ping the whole suite confirmed no test anywhere did.

Concretely: this project's own `stop_loss_setup` test dataset (already
used by three existing tests) reverses hard through a long's stop on its
final candle, `(open=104, high=105, low=90, close=92)` — the stop-loss
triggers on `low=90`, but `exit_price` was persisted as `92` (the close),
a value close enough to the entry that the row *understates* the real
loss; a milder reversal that still stops out but closes back above entry
would have made a genuine loss read as a gain in the trade journal.

Fixed by having `_maybe_exit` return the closing order's own
`average_fill_price` (read off `self.order_manager.get(order.id)` after
`execution_engine.submit`, the same object `orders.py`/`options.py` read
`final_order.average_fill_price` from) rather than reusing the
pre-execution `trigger_price` local or the candle's close. Threaded
through a new `PaperTradeOutcome.exit_price` field, consumed by both
`app/api/paper.py` and `app/workers/auto_trade_worker.py` in place of
`candle.close`/`latest.close`.

New tests:
`tests/api/test_paper.py::test_paper_trading_records_the_real_stop_price_not_the_candles_close`
and
`tests/workers/test_auto_trade_worker.py::test_supervisor_records_the_real_stop_price_not_the_candles_close`,
both reusing the existing `stop_loss_setup` dataset and asserting
`trade.exit_price == trade.stop` (never the candle's `92.0` close).
Verified both fail against the pre-fix code and pass once the fix is
restored.

## The backtest engine filled "retest" entries at prices the market never traded (§46-48)

`app/strategy/engine.py`'s `_resolve_entry_and_stop` computes a
`fvg_retest`/`order_block_retest` entry as the zone's midpoint
(`(gap.top + gap.bottom) / 2`) the moment an unmitigated FVG/order block
exists (`evaluate_conditions` only checks "does one exist", via
`smc.unmitigated_fvgs(...)`/`active_order_blocks(...)`) — not once price
has actually traded back into it. That's by design at the strategy-engine
layer: "unmitigated" genuinely means "not yet filled," and a fresh gap is
unmitigated the instant it forms, before price has had any chance to
retest it.

The bug was one layer up, in `app/backtest/engine.py`'s `run`/`_open_trade`:
it took `result.entry` from a matched signal and filled a simulated
position there **unconditionally**, with no check that the current
candle's own `[low, high]` range ever traded through that price. A
"retest" entry is supposed to mean "wait for price to come back to this
zone, then enter" — but the backtest engine matched and filled on the very
candle the gap *formed*, at a price that candle may never have touched at
all. Concretely, in this project's own `SETUP` test fixture, a bullish FVG
forms on candle index 7 (traded range `[106, 110]`) with gap midpoint
`103.0` — a price index 7's low of `106` never reaches — while the actual
retest only happens one candle later, at index 8 (`range [103, 109]`,
matching that dataset's own long-standing comment, `# retraces into the
FVG -> entry`). The backtest engine opened the trade a full candle early,
at an out-of-range phantom price, for every `fvg_retest`/
`order_block_retest` strategy — a first-class, documented DSL entry
style this platform is built around — silently distorting backtest P&L
(wrong entry price, wrong entry timing, and since `stop`/`target` are
computed relative to `entry`, wrong risk-reward too) for exactly the kind
of strategy backtesting exists to validate before it's trusted for
live/auto-trading. `app/paper/engine.py` and `app/replay/engine.py` are
unaffected — both fill at the candle's actual `close`, a genuinely traded
price, never at the zone's theoretical midpoint directly.

Existing tests didn't catch this because they only ever asserted
`trade.pnl > 0`/`trade.direction == "LONG"` on a dataset that still nets
a profit regardless of which candle (or which of two nearby prices) the
trade opened at, once price later rallies through the target.

Fixed with a minimal, direction-agnostic guard in `BacktestEngine.run`:
only call `_open_trade` when `candle.low <= result.entry <= candle.high`
for the matching candle — otherwise keep waiting for a later candle to
genuinely trade through the zone, the same way a real limit/retest order
would sit unfilled. For the default market-entry type (`entry ==
candle.close`), this check is always true, so it changes nothing for
strategies that don't use a retest-style entry.

New `tests/backtest/test_engine.py::test_backtest_only_fills_a_retest_entry_once_price_actually_trades_there`
reuses the existing `SETUP` fixture and asserts the trade opens at
candle index 8 (not 7) with `entry_price == 103.0` — the genuine retest,
not the phantom one. Verified the test fails against the pre-fix code
(it opens at index 7 instead) and passes once the fix is restored.

## `ScannerWorker` wrote a fresh `Signal` row and re-published to `/ws/signals` on every pass a match stayed valid (§28-29, §66)

`ScannerWorker._persist_new_setups` (raw SMC pattern detections, blueprint
§9's `setups` table) already dedups correctly: it explicitly reads back
existing `(setup_type, detected_at)` pairs before inserting, so re-scanning
the same historical candles on every pass never duplicates a row. Its
sibling `_evaluate` — the function that turns a strategy match into a
`Signal` row and a `/ws/signals` publish — had no such guard at all: every
single pass where `outcome.matched` was `True` unconditionally wrote a new
`Signal` and re-published, with zero check for "have I already recorded
this exact match."

That matters because `ScannerWorker` runs on a fixed wall-clock cadence
(`interval_seconds=60.0` in production, `app/workers/main.py`) against a
candle timeframe that changes far less often (`timeframe="15m"`) — the
same closed candle set gets re-evaluated roughly 15 times before the next
candle even closes. Many strategy conditions stay satisfied for as long as
the underlying structure persists — `{"type": "fvg", "direction":
"bullish"}` (checked via `smc.unmitigated_fvgs(...)`) is true for as long
as that gap remains unfilled, often many bars. So one genuine trading
setup wrote a fresh, near-identical `Signal` row and fired a fresh
`/ws/signals` event on every single pass for as long as it stayed
valid — flooding `GET /signals`'s 200-row window (potentially pushing
genuinely distinct, older signals out of it entirely) and spamming every
connected client with the same "new signal" alert once a minute, training
users to ignore exactly the notifications live scanning exists to
surface.

Fixed the same way `_persist_new_setups` already does it, adapted to
`Signal`'s shape (which has no fixed `detected_at` the way a raw SMC event
does — a signal's "identity" is its computed trade parameters): before
writing, `_evaluate` now reads back the most recent `Signal` for this
`(instrument_id, strategy_id)` pair and skips the insert/publish when its
`direction`/`entry`/`stop`/`target` are unchanged from the new match — the
zone actually shifting (a new gap, a different level) still writes a new
row, since that's a genuinely new opportunity, not a repeat.

New `tests/workers/test_scanner_worker.py::test_scanner_dedups_signals_across_passes`
mirrors the existing `test_scanner_persists_setups_and_dedups_across_passes`
test for `setups`: runs `run_once()` twice over the same unchanged candle
history and asserts exactly one `Signal` row exists both times (the same
row, by id). Verified it fails against the pre-fix code (a second row
appears after the second pass) and passes once the fix is restored.

## An out-of-order tick could silently corrupt candle history, and `upsert_candles` wasn't actually an upsert (§16, §66)

`CandleWorker.process_tick` (`app/workers/candle_worker.py`) decides
whether an incoming tick belongs to the candle currently forming or
starts a new one with a single check: `forming.timestamp != bucket_ts`.
That's correct for the ordinary case (a tick lands in a later bucket, so
the current candle closes and a new one opens) but wrong for a tick whose
bucket is *older* than the one currently forming — a completely ordinary
occurrence on a real live feed: a WebSocket reconnect routinely redelivers
a handful of already-seen ticks, and network jitter can deliver ticks out
of order. `!=` treats that stale tick exactly like a legitimate rollover:
it prematurely closes the *current*, correct, still-accumulating candle
with whatever partial data it had so far, and opens a bogus new "forming"
candle back at the old, already-closed bucket. When the next real tick
arrives, that bogus candle closes again and collides with the row already
persisted for that bucket.

That collision surfaced a second, independent bug: `app/market/repository.py`'s
`upsert_candles` — despite its name, and despite being the *only* place in
the codebase that ever writes a `candles` row (both the base-timeframe
path and every derived-timeframe recompute route through it) — was a bare
`INSERT` with no conflict handling at all. Any re-write of an
`(instrument_id, timeframe, timestamp)` combination that already existed
(the stale-tick scenario above, but also a worker restart replaying a
backfill, or a derived-timeframe recompute racing a previous one) raised
`asyncpg`'s `UniqueViolationError` against `uq_candle_key`. That exception
propagated out of `process_tick` into `app/workers/main.py`'s generic
`except Exception: logger.exception(...)` around the whole market-data
pipeline — silently logged and swallowed, with the triggering tick dropped
from `CandleWorker._forming` entirely (the exception fires before that
dict is updated), so the worker's in-memory bookkeeping for that symbol
was lost along with the write. Net effect: a single stale/duplicate tick
after an ordinary feed reconnect could truncate one real candle to a
single tick and silently drop a persistence write, with zero record
anywhere that it happened — and every downstream consumer (SMC/ICT
analysis, the scanner, backtests, charts) reads that corrupted/gapped
history from then on.

Fixed both halves:
- `process_tick` now only rolls over to a new forming candle when
  `bucket_ts` is strictly *newer* than the one currently forming. A tick
  whose bucket is older is dropped (with a warning log) rather than
  corrupting the current candle or reopening an already-closed one — the
  closed history for that bucket is already correct.
- `upsert_candles` is now a genuine `INSERT ... ON CONFLICT (instrument_id,
  timeframe, timestamp) DO UPDATE` (matching `uq_candle_key`), so any
  re-write of the same bucket — from any source, not just this one
  pathway — is a safe, idempotent overwrite instead of a crash.

New tests:
`tests/workers/test_candle_worker.py::test_process_tick_drops_a_stale_out_of_order_tick`
feeds a stale tick mid-way through forming a later candle and asserts it's
dropped (`process_tick` returns `None`), the current candle's OHLC is
untouched, and exactly the two genuine candles end up persisted — no
crash, no phantom third row.
`tests/api/test_markets_candles.py::test_upsert_candles_is_idempotent_on_conflict`
calls `upsert_candles` twice for the identical bucket with different OHLC
values and asserts the second call overwrites rather than raising. Verified
both fail against the pre-fix code (the first with a `UniqueViolationError`
propagating out of `process_tick`, the second directly) and pass once the
fix is restored.

## `ReconciliationWorker` re-fired its audit+notification alert on every pass an outage/mismatch stayed unresolved (§74-75)

`ReconciliationWorker.run_once` has two failure branches — the broker
being unreachable entirely (`except (BrokerError, httpx.HTTPError,
NotImplementedError)`) and a local/broker state mismatch (`if not
report.in_sync`). Both unconditionally called `halt_account(...)`,
`record_audit(...)`, and `create_notification(...)` every single time they
were reached, with no check for whether the account was *already* halted
for this same, still-unresolved incident.

That matters because `live_reconciliation.run()` calls
`reconcile_all_connected_accounts()` — which drives this worker — every 60
seconds in an infinite loop for as long as an account stays connected, and
by this module's own documented design, resuming is a **deliberate manual
admin action**, never automatic ("a mismatch means something needs a human
look"). So a broker outage or a state mismatch that isn't noticed and
resolved within a minute caused this worker to write a brand-new
`AuditLog` row and a brand-new `Notification` row (`BROKER_DISCONNECTED`
or `RECONCILIATION_REQUIRED`) once every single pass, indefinitely, for
the entire duration of the incident. This is the exact same defect *shape*
as `ScannerWorker`'s `Signal`-row spam fixed earlier in this document, just
in a different worker and subsystem — a sustained, real outage (exactly
the scenario blueprint §74 "Broker Failure Handling" exists to make
visible) flooded `GET /notifications` and the audit trail fastest, right
when a clean, singular, actionable alert mattered most.

Fixed by checking `account_halt_reason(self.account_id)` before writing
the audit row and notification in both branches: `halt_account` itself is
still called on every pass (cheap, idempotent, keeps the halt reason
fresh), but the `AuditLog`/`Notification` write only fires when the
account wasn't already halted — i.e. this is a newly-detected incident,
not a repeat of one still awaiting an admin's `resume_account` call. Once
resumed, `account_halt_reason` returns `None` again, so the next
occurrence still alerts exactly as before.

New `tests/workers/test_reconciliation_worker.py::test_reconciliation_does_not_repeat_the_mismatch_alert_on_every_pass`
and `::test_reconciliation_does_not_repeat_the_broker_unreachable_alert_on_every_pass`
each call `run_once()` twice in a row over the same unresolved
condition (no `resume_account` in between) and assert exactly one
`AuditLog` row and one `Notification` row exist afterward, not two.
Verified both fail against the pre-fix code and pass once the fix is
restored.

## Missing TRADE_EXECUTED/POSITION_CLOSED notifications on live orders and options

`POST /orders` and `POST /options/execute` are the two live-trading entry
points, and both already notified the user when a *proposal was rejected*
by the risk engine (`ORDER_REJECTED`, added in an earlier round). Neither
one, however, ever created a `Notification` row when the order actually
succeeded — an order that opened a new position, added to one, or closed
one out silently updated the database and returned an HTTP response, with
nothing appearing in `GET /notifications` or on any subscribed websocket
channel. This was a real parity gap against the paper-trading and
auto-trade paths (`app/api/paper.py`, `app/workers/auto_trade_worker.py`),
which already fire `NotificationType.TRADE_EXECUTED` on entry and
`POSITION_CLOSED` (or `SL_HIT`/`TP_HIT`) on exit — a user trading live
capital was worse-informed than one running the paper simulator.

Fixed by adding the same two notification sites to both live paths, using
existing signals already computed in each handler rather than new state:

- `app/api/orders.py`: `POST /orders` already computes `position_after`
  (the position after this order settles) and `realized_delta` (nonzero
  only when this order closed out or reduced an existing position). A new
  `just_filled`/`opened_or_added` guard — identical in spirit to the one
  already used elsewhere in this file to avoid mistaking a broker-rejected
  order for a fill — distinguishes "this call actually opened or added to
  a position" (`created and final_order.status in {FILLED,
  PARTIALLY_FILLED, MONITORING}` and the resulting position is open in the
  requested direction) from a same-direction order that the broker
  rejected, which would otherwise still see `position_after` reflecting
  the unchanged prior position and falsely look like a fill. `TRADE_EXECUTED`
  fires when `opened_or_added` is true; `POSITION_CLOSED` fires whenever
  `realized_delta != 0`, using the same `realized_delta` value already
  used to update `daily_pnl`/`weekly_pnl` and to call `record_trade`, so
  the notification body reports the same real fill price the journal
  entry does (see the earlier fill-price-accuracy fix in this document).
- `app/api/options.py`: `POST /options/execute` applies the identical
  pair of checks per leg, using `leg.direction`/`leg.symbol` in place of
  `payload.direction`/`payload.symbol`.

There is deliberately no `SL_HIT`/`TP_HIT` distinction on either live
path, unlike paper/auto-trade: those two notification types exist because
the paper engine and auto-trade supervisor themselves evaluate stop-loss
and take-profit levels bar-by-bar and know which one triggered a given
exit. No equivalent live stop-loss/take-profit enforcement worker exists
yet (a limitation already called out elsewhere in this document and in
`docs/PRODUCTION_READINESS.md`) — every live position close, for whatever
reason the caller closed it, is reported as the generic `POSITION_CLOSED`,
which is accurate to what the system actually knows.

New `tests/api/test_orders.py::test_live_order_notifies_on_trade_executed_and_position_closed`
opens a LONG position (asserts exactly one `TRADE_EXECUTED` notification)
and then closes it with an opposing SHORT order (asserts a second
notification, `POSITION_CLOSED`, whose body contains the real realized
P&L). New `tests/api/test_options_execute.py::test_execute_notifies_on_trade_executed_and_position_closed`
does the equivalent for a two-leg bull call spread closed by its exact
reversal (bear call spread), asserting two `TRADE_EXECUTED` notifications
on open and two more (`POSITION_CLOSED`, one per leg) on close. Both were
verified to fail against the pre-fix code and pass once the fix is
restored.

This change also exposed a latent gap in two unrelated, previously-passing
tests: `tests/api/test_portfolio.py` and
`tests/api/test_admin_portfolio_snapshot.py` both place a successful live
order as part of their setup, and their `_cleanup()` helpers delete the
test `User` row without first deleting any `Notification` rows for that
user — harmless before this fix, since a successful order never wrote a
notification, but a `notifications_user_id_fkey` foreign-key violation
once it started doing so. Both cleanup helpers now delete `Notification`
rows before deleting the `User` row, matching the ordering already used
for every other FK-dependent table in those same helpers.

## Upstox adapter's 200-with-error-envelope leaking a raw exception and permanently wedging the order

`UpstoxBroker.place_order` (`app/brokers/upstox/adapter.py`) is the one
place in the whole system where a real broker's HTTP response becomes a
live order's fate, and it had a gap in exactly the failure mode that
matters most: an ordinary broker-level rejection (insufficient margin,
market closed, invalid instrument) crashing the request instead of
producing a normal rejected order.

Upstox, like most broker APIs, can return **HTTP 200** with a
`{"status": "error", "errors": [...]}` body for this kind of failure —
this module's own `_unwrap` helper already detects that shape and raises
`BrokerError` for it (used correctly everywhere else in this adapter:
`get_account`, `get_positions`, `get_orders`, `get_quote`,
`get_option_chain` all let it propagate, because a lookup failing is
supposed to be an exception). `place_order`, however, is different: its
contract (now written down explicitly on `Broker.place_order` in
`app/brokers/base.py`) is that a broker-level rejection must come back as
an `OrderResult(status=REJECTED, ...)`, not raise — and the code only
caught `httpx.HTTPStatusError` (a 4xx/5xx status), never the `BrokerError`
that the exact same rejection produces when Upstox reports it via a 200
status with an error-shaped body instead.

The failure this produced was worse than a bad error message. Before
`ExecutionEngine.submit` (`app/trading/execution.py`) ever calls
`place_order`, the order has already been registered in
`OrderManager.create_order` under its idempotency key
(`app/trading/order_manager.py`) and transitioned to `SUBMITTED`. Neither
`ExecutionEngine.submit` nor its callers (`POST /orders`, `POST
/options/execute`) wrap that call in a `try`/`except`, so an uncaught
`BrokerError` propagated all the way to FastAPI's generic exception
handler — the client got a bare 500, with no order row ever persisted to
Postgres and no notification. Worse, because the idempotency key was
already mapped to this order *before* the broker call, retrying the exact
same order (same user/symbol/direction/entry/stop) hit
`create_order`'s `created=False` short-circuit and never called
`place_order` again — the order was permanently stuck at `SUBMITTED` in
the in-memory `OrderManager`, invisible in the database, un-retriable,
until the process restarted.

Fixed by adding an `except BrokerError` alongside the existing
`except httpx.HTTPStatusError` in `place_order`, returning the same
`OrderResult(status=REJECTED, rejection_reason=str(exc))` shape either
way — this is exactly what `ExecutionEngine.submit` already knows how to
handle (transitions the order to `REJECTED`, a legitimate terminal state
for that idempotency key), and flows through the same `ORDER_REJECTED`
notification/audit logic on `POST /orders`/`POST /options/execute` that a
4xx-style rejection already used. The `Broker.place_order` abstract
method's docstring now states this contract explicitly, since the sibling
`DhanBroker` adapter is still a stub (`raise NotImplementedError` on every
method) and will need the same care once implemented.

New `tests/brokers/test_upstox_adapter.py::test_place_order_200_error_envelope_returns_rejected_result_not_an_exception`
mocks a 200 response with an error-shaped body and asserts `place_order`
returns a normal `REJECTED` `OrderResult` rather than raising. Verified to
fail (with an unhandled `BrokerError`) against the pre-fix code and pass
once the fix is restored.

## The three-level kill switch (blueprint §58) was permanently a no-op

`RiskEngine.evaluate` and `evaluate_options_risk` (`app/risk/engine.py`,
`app/risk/options_risk.py`) have always checked a `kill_switch` first,
before any other risk check — an operator's ability to stop a
misbehaving strategy, freeze one account, or halt everything is meant to
be the single highest-priority veto in the whole risk pipeline (blueprint
§58). What existed, though, was `KillSwitchState`
(`app/risk/kill_switch.py`): a plain in-memory `@dataclass` with
`kill_global()`/`kill_account(id)`/`kill_strategy(id)` mutator methods
that **nothing outside a unit test ever called**. Every real call site
(`RiskEngine.__init__`, `evaluate_options_risk`) did
`kill_switch = kill_switch or KillSwitchState()` — always taking the
`or` branch, since nothing ever passed one in — producing a brand-new,
permanently-empty instance. There was no admin endpoint, no user
endpoint, no worker, nothing anywhere in the API that ever mutated a
`KillSwitchState` a live `RiskEngine` actually consulted. The
`"kill_switch"` `RiskCheck` passed unconditionally, in every environment,
regardless of operator intent — and even if some code path had called
`kill_global()` on some object, it couldn't have reached anything: each
per-user `_UserTradingStack` (`app/api/orders.py`) builds its own
`RiskEngine` with its own fresh `KillSwitchState`, and `PaperTradingEngine`
does the same, so a kill on one in-memory instance is invisible to every
other stack in the same process, let alone another process. This is the
identical cross-process gap an earlier round already fixed for
reconciliation-triggered account halts — in fact `app/core/redis.py`'s
halt section carries a comment calling out `KillSwitchState` by name as
"can't carry this signal between processes" — but that fix was never
extended to the kill switch itself.

Fixed by giving the kill switch the same Redis-backed treatment as
account halts. `app/core/redis.py` gained `set_global_kill`/
`clear_global_kill`/`is_global_killed`, the equivalent trio for
`*_account_kill(account_id)` and `*_strategy_kill(strategy_id)`, and
`list_killed_accounts`/`list_killed_strategies` for visibility — all
simple `SET`/`DELETE`/`GET`/`SCAN` operations under `kill:global`,
`kill:account:<id>`, `kill:strategy:<id>` keys. `app/risk/kill_switch.py`
gained `load_kill_switch_state(account_id, strategy_id)`, an async
function that reads those keys back into a `KillSwitchState` — reusing
`KillSwitchState.is_blocked`'s existing logic unchanged rather than
rewriting it, and keeping `RiskEngine`/`evaluate_options_risk` themselves
synchronous and IO-free (their docstring is explicit: "AI ≠ Risk Manager
... only looks at deterministic account/market state" — the same
principle applies to this engine not owning IO itself). Every real
trading path now calls `load_kill_switch_state` and assigns the result to
`risk_engine.kill_switch` immediately before evaluating, mirroring how
`POST /orders` already re-checks `account_halt_reason` on every call
rather than once at stack construction: `app/api/orders.py`'s
`place_order`, `app/api/options.py`'s `execute_options_strategy` (passed
directly to `evaluate_options_risk`'s `kill_switch` parameter, since it's
a free function rather than a method), and `app/paper/engine.py`'s
`on_candle` — which `app/workers/auto_trade_worker.py`'s
`AutoTradeSupervisor` already reuses, so this one change covers both the
manual paper-trading API and the autonomous trading loop.

New admin endpoints in `app/api/admin.py` make the switch operable at
all: `GET /admin/kill-switch` (current global/account/strategy state),
`POST`/`DELETE /admin/kill-switch/global`, `.../account/{account_id}`,
and `.../strategy/{strategy_id}` — mirroring the existing
`GET /admin/halted-accounts`/`POST /admin/accounts/{id}/resume` pattern,
including the same `confirm: true` requirement on every trigger and an
audit-log row (`admin.kill_switch_*_triggered`/`*_cleared`) on every
mutation.

New `tests/api/test_admin.py::test_admin_can_view_and_trigger_the_three_level_kill_switch`
exercises the full admin CRUD surface at all three levels. New
`tests/api/test_orders.py::test_account_kill_switch_blocks_a_new_order`
and `tests/api/test_options_execute.py::test_execute_respects_account_kill_switch`
each set a real Redis kill key via `set_account_kill` (not a manually
constructed `KillSwitchState`, which would prove nothing about whether
the real path reads Redis at all) and confirm the corresponding live
endpoint now rejects with a 403 and an `ORDER_REJECTED` notification, then
that clearing the key lets the account trade again. The existing
`tests/paper/test_engine.py::test_paper_engine_respects_risk_kill_switch`
previously set `engine.risk_engine.kill_switch` directly and asserted
`RiskEngine.evaluate` respected it — true, but no longer representative,
since `on_candle` now overwrites `kill_switch` from Redis on every candle
regardless of what was set beforehand; it was rewritten to go through
`set_global_kill`/`clear_global_kill` instead, so it actually exercises
the wiring rather than just the already-correct pure `is_blocked` logic.
All four were verified to fail against the pre-fix code (three with a
collection-time `ImportError` for the not-yet-existing Redis functions,
the fourth — the rewritten paper-engine test — the same way) and pass
once the fix is restored.

## Cancelling a live order could 500 the request and permanently wedge the order

`UpstoxBroker.place_order` was hardened in an earlier round to catch both
`httpx.HTTPStatusError` (a 4xx/5xx) and `BrokerError` (Upstox's own
HTTP-200-with-`{"status":"error"}` envelope, raised by this adapter's
`_unwrap` helper) and turn either into a normal `OrderResult(status=
REJECTED, ...)` instead of letting them propagate. `cancel_order` in the
same file (`app/brokers/upstox/adapter.py`) was never given the same
treatment: it called `response.raise_for_status()` and `_unwrap()` with no
`try`/`except` at all, so `httpx.HTTPStatusError` propagated straight out
uncaught (the 200-error-envelope shape already came out as `BrokerError`
via `_unwrap`, so that half was accidentally fine — only the 4xx/5xx half
was actually broken). The only caller, `POST /orders/{id}/cancel`
(`app/api/orders.py`), had no `try`/`except` around `stack.broker.
cancel_order(...)` either, and that call sat *before*
`order_manager.transition(order_id, OrderStatus.CANCELLED, ...)`.

The concrete failure: a user cancels an order that Upstox, in the
meantime, has already filled or already cancelled — an entirely ordinary
race between the client seeing a stale order state and clicking cancel.
Upstox reports that as an ordinary rejection (e.g. "Order already
complete"), most often as a 4xx. That exception used to propagate
uncaught past the `order_manager.transition` line, get caught only by
`main.py`'s generic `Exception` handler, and return a bare 500 — and
because the transition line never ran, the order stayed at its prior
`SUBMITTED`/`ACKNOWLEDGED` status in both the in-memory `OrderManager` and
the database forever, with no indication of what went wrong or what state
the order was really in. Every retry of the cancel endpoint hit the same
500 indefinitely.

Fixed by wrapping `UpstoxBroker.cancel_order` in the equivalent
`try`/`except`, normalizing an `httpx.HTTPStatusError` into a `BrokerError`
(so the 4xx/5xx and 200-error-envelope cases now surface identically) and
re-raising it rather than swallowing it into a synthetic `OrderResult`:
unlike `place_order`, there's no `OrderStatus` value that cleanly
represents "failed to cancel" from *both* the `SUBMITTED` and
`ACKNOWLEDGED` source states the endpoint accepts (`app/trading/
order_manager.py`'s transition table allows `SUBMITTED -> FAILED` but not
`ACKNOWLEDGED -> FAILED`), so forcing a status here risked trading one
crash for an `IllegalTransitionError` on exactly the case that matters.
Instead, `POST /orders/{id}/cancel` now catches `BrokerError` around the
broker call and returns a clean `502 Bad Gateway` with the broker's reason,
skipping the transition/persist/audit lines entirely — the order is left
exactly as it was, which is the truth (the broker never actually cancelled
it), and existing reconciliation is what should correct the local state if
the broker's own status has genuinely moved on.

New `tests/brokers/test_upstox_adapter.py::test_cancel_order_4xx_raises_broker_error_not_http_status_error`
(the one that actually needed the fix) and
`::test_cancel_order_200_error_envelope_raises_broker_error` (already
correct pre-fix, kept as a regression guard on that path too) cover the
adapter directly. New `tests/api/test_orders.py::
test_cancel_order_broker_failure_is_surfaced_cleanly_and_leaves_status_unchanged`
exercises the full endpoint with a broker double whose orders never fill
(`MockBroker`'s always do, immediately, so there'd be no way to reach a
still-cancelable order otherwise) and whose `cancel_order` always fails,
asserting a 502 (not 500) and that the order's status is still
`ACKNOWLEDGED` afterward via `GET /orders`, never silently marked
`CANCELLED` or lost. All three new/relevant assertions were verified to
fail against the pre-fix code (a bare 500 for the endpoint test, an
uncaught `httpx.HTTPStatusError` for the 4xx adapter test) and pass once
the fix is restored.

`modify_order` in the same adapter file has the identical shape (no
`try`/`except` around `raise_for_status()`/`_unwrap()`) but currently has
zero callers anywhere in this codebase, so it isn't a live bug — left
unchanged rather than fixed speculatively; whoever wires it up should
apply the same pattern.

## Paper trading and the autonomous trading loop never persisted an open Position row

`app/trading/persistence.py`'s `persist_position` is how an order placed
through `POST /orders` or `POST /options/execute` mirrors its position
into the `positions` table — the shared source of truth `GET /portfolio`
(blueprint §9), `POST /admin/portfolio-snapshot`, and the
correlated-exposure risk check (`app.risk.portfolio.compute_correlated_exposure`,
blueprint §85-86) all read from. A grep across the whole `app/` tree for
`persist_position(` turned up exactly two call sites — `app/api/orders.py`
and `app/api/options.py` — and nothing else, ever. `app/api/paper.py`
(manual paper trading sessions, blueprint §49) and
`app/workers/auto_trade_worker.py`'s `AutoTradeSupervisor` (blueprint §54's
flagship fully-autonomous trading loop) both drive the exact same
`PaperTradingEngine`/`PositionManager`, and both already mirror *closed*
trades into the `trades` table (an earlier round's fix), but neither one
ever mirrored an *open* position into `positions` at all. A position this
engine opened was completely invisible outside its own in-memory
`PositionManager` for its entire open lifetime — `GET /portfolio` and
`POST /admin/portfolio-snapshot` reported the same exposure as if no
autonomous or paper trade were open, no matter how large one actually
was, until the moment it closed and a `Trade` row finally appeared.

This is worse than an isolated blind spot for blueprint §54's headline
feature specifically: `AutoTradeSupervisor` builds one independent
`PaperTradingEngine` (with its own `MockBroker` balance and its own fresh
`PositionManager()`) per `(user, strategy, instrument)` key, so a user
running several auto-trading strategies concurrently has several
completely isolated in-memory position registries with no cross-engine
visibility into each other even in principle — the `current_exposure`/
`strategy_allocation` fields `PaperTradingEngine.on_candle` builds into
its `TradeRiskProposal` are hardcoded to `0.0`, and `correlated_exposure`
is left at its unset default, unlike `app/api/orders.py`'s live path,
which correctly sums real open-position notionals and calls
`compute_correlated_exposure`. Persisting every engine's position to the
same `positions` table (keyed by `user_id`/`instrument_id`/
`execution_mode`, not by which in-memory engine wrote it) is the
prerequisite for ever closing that gap — with nothing in the database,
there was no shared surface even a future fix to those hardcoded zeros
could read from. Actually wiring real cross-engine exposure computation
into `PaperTradingEngine.on_candle` remains open (would need those three
proposal fields computed from a DB query across the user's other open
`PAPER` positions, the same way `app/api/orders.py` already does it for
`LIVE` — a larger change deliberately left for a future round); this
round's fix makes the underlying data exist at all.

Fixed by calling `persist_position(db, user_id, instrument_id,
position_after, execution_mode=ExecutionMode.PAPER)` right after
`engine.on_candle(...)` returns, in both `app/api/paper.py`'s
`feed_candle` and `app/workers/auto_trade_worker.py`'s `_process` — on
every candle, not only when a position opens or closes, so mark-to-market
`unrealized_pnl` stays current the same way it would for a real broker
position, and `is_open` flips to `false` in the database the instant the
in-memory position actually closes.

New `tests/api/test_paper.py::test_paper_trading_persists_the_open_position_to_the_database`
feeds a manual paper session candles up to (but not through) its entry
signal, asserts a `Position` row exists with `execution_mode=PAPER`,
`is_open=True`, and the right instrument, then feeds the closing candle
and asserts `is_open` flips to `False`. New
`tests/workers/test_auto_trade_worker.py::test_supervisor_persists_the_open_position_to_the_database`
does the same through `AutoTradeSupervisor.run_once()`. Both verified to
fail against the pre-fix code (`scalar_one()` on an empty result — no
`Position` row exists at all) and pass once the fix is restored.

## Strategy DSL's `Condition.lookback` was fully documented and completely ignored

`Condition` (`app/strategy/dsl.py`) carries a `lookback: int = 5` field
with an inline comment describing exactly what it's for: "how many recent
candles/events count as recent for event-type conditions." The class
docstring backs this up — "the remaining fields are interpreted by that
evaluator." A grep of the whole `app/` tree for `lookback` outside that
one file and unrelated helpers (`order_blocks.py`'s internal averaging
window, `portfolio.py`'s correlation window) turned up nothing:
`app/strategy/evaluator.py`, the only consumer of `Condition`, never once
read `condition.lookback`.

This matters specifically for the "event-type" conditions the field's own
comment calls out: `BOS`/`CHOCH`/`MSS` (`evaluate_condition` matched
against `smc.structure_events`/`smc.mss_events` — the full list
`SMCEngine.analyze` ever detected across the entire visible candle
history) and `LIQUIDITY_SWEEP` (matched against `smc.recent_sweeps()`,
whose name implies recency but which returns every swept
`LiquidityPool` ever, unfiltered). Unlike `FVG`/`ORDER_BLOCK`, whose
"unmitigated"/"active" checks are genuine persistent *states* that
legitimately stay true until something invalidates them, a BOS/CHoCH/MSS
break or a liquidity sweep is a one-time historical *event* — it has no
other expiry mechanism. A strategy with `Condition(type=BOS,
direction="bullish")` would keep matching on every single future candle,
forever, the instant any bullish break of structure ever appeared in the
candle history handed to the SMC engine — for live scanning/auto-trading,
whose candle history only grows, this meant a structurally stale setup
from days or weeks earlier could still fire an entry today, with the
`lookback=5` a strategy author configured (or the blueprint §33-34 AI
strategy generator produced) having zero effect on when it stopped
counting as "recent."

Fixed by adding `EvaluationContext.current_index` — the index of the
current (most recent) candle within the exact same candle list `smc`/`ict`
were computed from, matching the indexing convention `StructureEvent.index`
and `LiquidityPool.swept_index` (`app/smc/types.py`) already use, since
every SMC detector only ever looks inside the single candle list it's
given (`app/smc/engine.py`'s own docstring: "every detector below only
ever looks inside the list it is given"). `evaluate_condition`'s
BOS/CHoCH/MSS and `LIQUIDITY_SWEEP` branches now filter their matches to
`context.current_index - event.index < condition.lookback` before
checking anything else. All five `EvaluationContext` construction sites
(`app/paper/engine.py`, `app/backtest/engine.py`, `app/api/ai.py`,
`app/api/scanner.py`, `app/workers/scanner_worker.py`) now pass
`current_index=len(candles) - 1`. The field defaults to `0` for
any caller that doesn't set it (there shouldn't be one left), which fails
open to the old, always-matches behavior rather than silently rejecting a
genuinely recent event should some caller be missed.

New `tests/strategy/test_evaluator.py` (previously no dedicated test
module for `app.strategy.evaluator` existed at all) covers both fixed
branches directly: `test_bos_condition_expires_after_its_lookback_window`
reuses the exact bullish-BOS-at-index-7 fixture already pinned by
`tests/smc/test_structure.py`, asserting the same `Condition` matches when
evaluated shortly after the break (`current_index = bos.index + 4`,
within the default `lookback=5`) but not many candles later
(`current_index = bos.index + 20`).
`test_liquidity_sweep_condition_expires_after_its_lookback_window` does
the equivalent for a swept buy-side liquidity pool, reusing
`tests/smc/test_liquidity.py`'s `EQUAL_HIGHS` fixture. Both verified to
fail against the pre-fix code (`EvaluationContext.__init__()` rejecting
the then-nonexistent `current_index` keyword — the test file could not
even be written against the old signature) and pass once the fix is
restored.

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
