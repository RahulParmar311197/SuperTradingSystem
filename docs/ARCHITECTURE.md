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
(`_refuse_unsafe_defaults_in_production`) that raises a clear `ValueError`
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
remediation. `device_info` was believed at the time to be "written at
every login and never read back — collected, but write-only". That
diagnosis was exactly inverted, and the section near the end of this
document on device tracking records what was actually true.

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
  (not revoked, not expired), surfacing `device_info`. This is the first
  code in the repository that ever reads that column back — though, as it
  turned out, it had nothing to read.
- **`POST /auth/sessions/{id}/revoke`** — revokes a specific session by
  id, ownership-checked the same way `/paper/*` and `/replay/*` sessions
  are: a non-owner gets `404`, never a `403` that would confirm the
  session exists at all.

`tests/api/test_auth_sessions.py` proves: logging out actually invalidates
the refresh token (a subsequent `/auth/refresh` with the same token
returns `401`); logout is idempotent for an already-revoked session and
tolerates a garbage token without raising; `GET /auth/sessions` shrinks
once a listed session is revoked (it was claimed to prove that the
endpoint "reflects `device_info`", which it did not — see below); a non-owner
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

## A strategy could use AND/OR/NOT operators the DSL declares but silently could never fire

Blueprint §33 lists the Strategy DSL's operators as `AND, OR, NOT,
GREATER_THAN, LESS_THAN, CROSSES, TOUCHES, WITHIN`, and `ConditionOperator`
(`app/strategy/dsl.py`) faithfully declares all eight — but
`_numeric_compare` in `app/strategy/evaluator.py`, the only place
`Condition.operator` is ever read, only branches on
`GREATER_THAN`/`LESS_THAN`/`WITHIN`/`TOUCHES`/`CROSSES`. `AND`/`OR`/`NOT`
had no handling anywhere in the codebase and fell through to
`evaluate_condition`'s final `return False`, unconditionally, regardless
of the actual indicator value being compared.

This was reachable, not just a theoretical schema/implementation
mismatch: `app/ai/strategy_builder.py`'s system prompt tells the AI
"Only use condition types, operators, and entry types the schema
defines" — actively inviting it to use `NOT`/`AND`/`OR` since they're
right there in the schema — and `StrategyDefinition.model_validate` (the
backend's sole validation gate, per that file's own docstring) accepted
them with no error. A strategy like "RSI NOT within [40, 60]"
(`type=indicator, name=rsi, operator=NOT, min_value=40, max_value=60`)
passed validation cleanly, looked like a normal persisted strategy, and
would never fire on any candle, ever — with nothing anywhere (no log, no
error, no field) indicating why. That's a worse failure mode than a
crash for a trading system: it silently wastes a trader's or the AI
strategy generator's effort on something that looks like it should
eventually trigger but structurally never can.

Implementing real boolean composition properly is a larger redesign than
this fix warrants: `Condition` is a flat leaf (`StrategyDefinition.conditions`'s
own comment says "implicit AND across the list," and blueprint §34's
worked example only ever shows that flat-list form) — `AND`/`OR` are
naturally combinators over multiple conditions, not a single leaf's
comparison mode, so supporting them would mean restructuring the DSL to
support nested condition groups, not just adding a branch to
`_numeric_compare`. Given that ambiguity and scope, the fix here is
honesty rather than a speculative redesign: `Condition` now has a
Pydantic field validator that rejects `operator in
{AND, OR, NOT}` at validation time with a clear message, so a strategy
using one of them fails loudly — a 422 from `POST`/`PUT /strategies`, or
a `StrategyBuilderError` from `app.ai.strategy_builder.parse_strategy_json`
(which already catches `pydantic.ValidationError` and re-raises with the
AI's raw output attached) — instead of being silently accepted into a
strategy that can never trigger.

New `tests/strategy/test_dsl.py` (previously no dedicated DSL-level test
module existed) parametrizes over all three unimplemented operators,
asserting `Condition(...)` raises `ValidationError` with a message
mentioning "not yet implemented," and a sibling test confirms the three
genuinely-implemented operators (`GREATER_THAN`/`LESS_THAN`/`WITHIN`)
still construct normally. Verified to fail against the pre-fix code (no
`ValidationError` raised at all for `AND`/`OR`/`NOT`) and pass once the
fix is restored.

## AI endpoints only handled "no provider configured," not "provider configured but the call failed"

`app/ai/providers/anthropic_client.py`'s `complete_json` can fail in two
ways beyond simply not being configured: `_extract_json` raises
`AIResponseParseError` (a `ValueError` subclass) whenever Claude's text
isn't valid JSON — an ordinary LLM failure mode (extra prose, a
truncated completion, a refusal sentence), not a hypothetical — and the
underlying `AsyncAnthropic.messages.create` call can itself raise any
`anthropic` SDK exception (`RateLimitError`, `APITimeoutError`,
`APIConnectionError`, ...) with nothing anywhere normalizing those into a
catchable, domain-specific type.

`POST /ai/propose-trade` and `POST /ai/chat` (`app/api/ai.py`) only ever
caught `AIUnavailableError` — raised solely by `NullAIClient` when no
provider is configured at all. In a real deployment (`AI_PROVIDER=anthropic`
with a key set), that's the *rare* failure mode; a rate limit or a
malformed completion is the realistic one, and neither was caught. Both
fell through to `main.py`'s app-wide generic exception handler, returning
a bare 500 — and because the exception propagated before either
endpoint's own audit-write code ran, no `AIDecision` row (`propose_trade`)
or assistant `AIMessage` row (`chat`) was ever written. This directly
contradicts `propose_trade`'s own docstring: "the outcome is persisted as
an `AIDecision` row (blueprint §71 audit logging, §79 'AI Model
Evaluation' — you can't evaluate AI behavior over time without a record
of what it actually said)" — a promise kept only for the least likely
failure mode. For `chat` specifically, the earlier `db.add(AIMessage(role=
"user", ...))` was also never committed before the uncaught exception, so
the user's own message vanished from `GET /ai/chat/history` too, with no
visible reply anywhere.

Fixed by adding `AIProviderError` (`app/ai/client.py`) — a shared
exception any `AIClient` implementation should raise for "the call to the
provider itself failed," distinct from `AIUnavailableError` (no provider
configured) and from a `ValueError`/`AIResponseParseError` (the provider
answered, but its content wasn't usable). `AnthropicAIClient.complete_json`
now wraps the `messages.create` call in `try`/`except anthropic.APIError`
(the base class every SDK exception — rate limits, timeouts, connection
errors, non-2xx statuses — subclasses) and re-raises as `AIProviderError`.
`propose_trade`, `chat`, and `build_strategy_endpoint` (which had the
identical gap: it already caught `ValueError` for parse failures but not
`AIProviderError` for API failures) now catch `(AIUnavailableError,
AIProviderError, ValueError)` together, writing the same audit row the
`AIUnavailableError` branch already did and returning a clean `502 Bad
Gateway` (`503` stays reserved for the true "no provider configured"
case) instead of an opaque 500.

New `tests/ai/test_anthropic_client.py::test_complete_json_wraps_api_errors_as_ai_provider_error`
monkeypatches `messages.create` to raise `anthropic.APIConnectionError`
and asserts `complete_json` raises `AIProviderError` instead. New
`tests/api/test_ai_propose_trade.py::test_propose_trade_returns_502_and_records_a_decision_when_ai_provider_call_fails`
and `tests/api/test_ai_chat.py::test_chat_returns_502_and_persists_both_messages_when_ai_provider_call_fails`
exercise both endpoints end-to-end with a fake `AIClient` that raises
`AIProviderError`, asserting a 502 and that the audit row (`AIDecision`/
both `AIMessage` rows) is actually written. All three verified to fail
against the pre-fix code (a collection-time `ImportError` for the
not-yet-existing `AIProviderError`) and pass once the fix is restored.

## Higher-timeframe candle derivation corrupted an already-closed bucket on a base-timeframe gap

`app/workers/candle_worker.py`'s `CandleWorker` closes base-timeframe (1m)
candles as ticks arrive and, once a base candle completes a higher
timeframe's bucket boundary (`_completes_bucket`, purely wall-clock
arithmetic on the closing candle's timestamp — it has no notion of whether
every base candle inside that bucket actually exists), calls
`_derive_timeframe` to aggregate that bucket's base candles into one
derived candle (e.g. five 1m candles into one 5m candle).

`_derive_timeframe` picked which base candles to aggregate by fetching a
lookback window of recent base candles and slicing the last `window =
target_minutes // base_minutes` rows *positionally* (`recent[-window:]`),
then deriving the bucket's timestamp from `recent[0].timestamp`. This
assumed the base timeframe has no gaps. In practice, a single dropped
tick, a worker restart, or a feed hiccup during one bucket leaves that
bucket with fewer base candles than `window` — but the *next* bucket's
closing candle still satisfies `_completes_bucket` by wall-clock
arithmetic and still triggers derivation. When that happens, the
positional slice pads the missing count out with base candles from the
*previous*, already-derived bucket. `recent[0]` then belongs to that
previous bucket, so the derived timestamp computed from it collides with
the previous bucket's already-persisted row (`upsert_candles` upserts on
`(instrument_id, timeframe, timestamp)`) — silently overwriting an
already-correct derived candle with data spanning two different periods,
while the true current (incomplete) bucket is never derived at all. A
naive `len(recent) < window` guard did not catch this, since the total
row count across both buckets combined still reached `window`.

Fixed by computing the target bucket's timestamp directly from `as_of`
(`target_bucket_ts = compute_bucket_start(as_of, target_minutes)`, no
longer inferred from whichever row happens to land first in a positional
slice) and filtering the fetched base candles to bucket *membership*
rather than position: `[c for c in recent if compute_bucket_start(c.timestamp,
target_minutes) == target_bucket_ts]`. This is the same approach
`app/market/aggregation.py`'s `resample_candles` already uses correctly —
bucketing each candle by its own timestamp rather than assuming a gap-free
run of rows. If the filtered set doesn't contain exactly `window` candles,
the bucket is genuinely incomplete and derivation is skipped, leaving the
previous bucket's derived candle untouched.

New `tests/workers/test_candle_worker.py::test_derive_timeframe_skips_an_incomplete_bucket_instead_of_corrupting_the_prior_one`
derives a first 5m bucket normally from five 1m candles, then feeds a
second 5m bucket's worth of ticks with one minute deliberately missing.
It asserts the first bucket's derived candle (`open`/`close`) is still
exactly what it was before the second bucket's (still-firing, per
wall-clock boundary) derivation attempt — i.e. no second 5m row appears
and the first one isn't corrupted. Verified to fail against the pre-fix
code (the first bucket's derived candle gets silently overwritten with
`open=104.0` instead of the correct `100.0`, mixing in a base candle from
the second bucket) and pass once the fix is restored.

## Options execution's "market data fresh" risk check was structurally dead — it could never fail

`app/risk/options_risk.py`'s `evaluate_options_risk` implements a
`market_data_fresh` `RiskCheck` for the multi-leg options execution path
(`POST /options/execute`), the direct analogue of the equity order path's
own freshness guard (`app/api/orders.py` populates
`TradeRiskProposal.market_data_age_seconds` from
`app.core.redis.get_price_age_seconds` before calling `RiskEngine`). The
check itself is correct: `proposal.market_data_age_seconds <=
limits.market_data_max_staleness_seconds` (10s by default). But
`OptionsRiskProposal.market_data_age_seconds` defaults to `0.0`, and the
only place an `OptionsRiskProposal` is ever constructed
(`app/api/options.py`'s `execute_strategy`) never set it — unlike its two
sibling fields computed in the very same per-leg snapshot loop
(`liquidity_acceptable`, `premium_deviation_pct`), which the loop actually
populates from each leg's `OptionSnapshot`. Since `0.0` is always `<=
10.0`, `market_data_fresh` could never fail, for any input: it was
structurally dead code masquerading as a live check.

This mattered for more than "a check that never fires" — the decision's
outcome, including this specific check's `passed` value, is written
verbatim into the persisted `RiskEvent.checks` audit row
(`{c.name: c.passed for c in decision.checks}`). Every approved (and every
rejected-for-another-reason) options strategy therefore carried a
`"market_data_fresh": true` in its permanent audit trail, falsely
attesting that freshness had been verified — even for a leg whose
`OptionSnapshot` was hours stale, or one with no snapshot at all (the
`_latest_option_snapshot is None` branch, which the code's own comment
notes is the common case today: "this environment has no options-chain
ingestion pipeline yet"). A real order could be placed against arbitrarily
stale options-chain data with the audit log actively asserting the
opposite.

Fixed by computing a real per-leg staleness in that same loop —
`(datetime.now(timezone.utc) - snapshot.snapshot_at).total_seconds()` —
and taking the worst (max) value across every leg that has a snapshot,
mirroring `premium_deviation_pct`'s existing "worst case across legs with
real data" pattern exactly (including its documented "0.0 when no leg has
snapshot data yet" default, which intentionally avoids permanently
blocking this endpoint in an environment that has no ingestion pipeline at
all — the same tradeoff already made explicit for the liquidity check).
That computed value is now passed into
`OptionsRiskProposal(market_data_age_seconds=...)`, so a leg with an
actually-stale snapshot now correctly fails `market_data_fresh` and is
rejected, with an honest `false` recorded in the audit row — while an
order with fresh (or entirely absent) snapshot data behaves exactly as
before.

New `tests/api/test_options_execute.py::test_execute_rejects_when_the_real_quote_is_stale`
creates an `OptionSnapshot` 15 seconds old — past the 10s risk-engine
threshold but still under the liquidity filter's own separate 30s
quote-age threshold, so the failure is isolated to `market_data_fresh`
specifically — with `premium` set to match the snapshot's mid exactly (so
`premium_matches_market` also can't be the cause). Asserts a 403 with
"Data age" in the response, no `Order` row created, and the persisted
`RiskEvent.checks["market_data_fresh"]` is `False`. Verified to fail
against the pre-fix code (the same request returns 201 with the order
actually placed) and pass once the fix is restored, via `git stash`.

## AutoTradeSupervisor's per-instrument engines silently defeated account-wide position/exposure caps

`AutoTradeSupervisor` (`app/workers/auto_trade_worker.py`, blueprint §54's
flagship autonomous trading loop) evaluates every `(user, active strategy)`
pair against every active instrument, and `_process` lazily builds and
caches one `PaperTradingEngine` per `(user, strategy, instrument)` key.
Each `PaperTradingEngine` constructed its own private `PositionManager()`
(`app/paper/engine.py`) — so a user auto-trading one strategy across, say,
50 instruments ended up with 50 completely isolated position ledgers for
the same account, each of which could only ever know about the one
position for its own instrument.

`on_candle`'s risk proposal fed that isolation straight into the risk
engine: `open_positions=len(self.position_manager.open_positions(self.account_id))`
could only ever be 0 or 1 (this method already returns early above if
`self.symbol` itself has an open position, so it can never see more than
that), and `current_exposure` was hardcoded to `0.0` — not "computed from
whatever's available," never fed anything at all. `RiskLimits.max_open_positions`
and `max_exposure_pct` (the exact fields `POST /auto-trading/enable`
exposes to let a user cap how much unattended risk they're taking) were
checked every single time against this fictional, always-near-zero view.
Contrast with `_UserTradingStack` (`app/api/orders.py`), the equivalent
stack for the manual/live path, which already gets this right — it caches
exactly *one* `PositionManager` per user, shared across every symbol they
trade, so `open_positions()` genuinely aggregates.

Concretely: a user enables auto-trading with `auto_trading_max_positions =
5` and one strategy eligible for auto-trading, against 50 active
instruments. If that strategy's entry condition fires on many of them in
the same pass, every one of those engines independently evaluates
`open_positions (0 or 1) < 5` and `current_exposure (always 0.0) + this
trade's notional ≤ max_exposure_pct * balance` — both trivially true no
matter how many other positions this same user already has open elsewhere
— so most or all of them can open simultaneously. The account can end up
with far more concurrent (paper, for now) positions and aggregate notional
than the user configured, with the two caps meant to bound that silently
inert.

Fixed by giving `PaperTradingEngine.__init__` an optional
`position_manager` constructor argument (defaulting to a fresh private one
when not given, so every other caller — `app/api/paper.py`'s manual
sessions, which are deliberately isolated per session since each carries
its own `starting_balance` — is unaffected) and having
`AutoTradeSupervisor` maintain one shared `PositionManager` per user
(`self._position_managers`, keyed by `user.id`, mirroring
`_UserTradingStack`'s pattern exactly), passed into every engine built for
that user regardless of which instrument or strategy it drives.
`PositionManager` already keys its internal state by `(account_id,
symbol)`, so sharing one instance across many symbols for the same account
was already its intended use — nothing about `PositionManager` itself
needed to change. `on_candle`'s `current_exposure` is now actually
computed (`sum(abs(p.quantity) * p.average_price for p in
self.position_manager.open_positions(self.account_id))`) instead of a
hardcoded `0.0`, so it reflects every other currently-open position for
that account the moment the manager is shared.

New `tests/workers/test_auto_trade_worker.py::test_supervisor_caps_open_positions_account_wide_across_instruments`
sets `auto_trading_max_positions=1` for a user with one strategy, creates
a second instrument, and feeds the identical bullish setup to both
instruments in lockstep so both engines would independently match and try
to open on the exact same `run_once()` pass. Asserts exactly one `Position`
row ends up open, account-wide. Verified to fail against the pre-fix code
(two positions open simultaneously, since each engine's own
`open_positions` count never saw the other) via `git stash`, then pass
once the fix is restored.

## Live/manual positions never got marked to market, so unrealized P&L was a permanent lie

`PositionManager.mark_to_market` (`app/trading/position_manager.py`,
blueprint §60's Position Manager) correctly computes `unrealized_pnl =
(price - average_price) * quantity` — it just needs someone to call it
with a current price. `apply_fill`, called on every order fill by
`ExecutionEngine.submit`, only ever touches `quantity`/`average_price`/
`realized_pnl`; it has no reason to know the current market price, and
nothing calls `mark_to_market` for the live/manual order path either.
`PaperTradingEngine.on_candle` (`app/paper/engine.py`) is the *only*
caller anywhere in the codebase — it marks its own account's position to
market every candle. `POST /orders`/`POST /options/execute`'s
`_UserTradingStack`, which every real (or `MockBroker`-backed manual)
order goes through, never did the equivalent.

`GET /positions` (`app/api/positions.py`) and `GET /portfolio`
(`app/api/portfolio.py`) both read `unrealized_pnl` straight off that same
`PositionManager`, and `app.trading.persistence.persist_position` writes
it verbatim into the `positions` table. So a user who places a live
`LONG` order at ₹100, watches the market run to ₹120 (a real, sizeable
gain) or drop to ₹80 (a real, sizeable loss), gets back `unrealized_pnl:
0.0` from both endpoints indefinitely — not stale, not approximate, a
constant lie — until the position is closed and `realized_pnl` finally
picks up the true number via `apply_fill`'s closing-branch math. Every
consumer of these two endpoints (a dashboard, a risk summary, a
mobile client) was reading a number that looked authoritative but had no
relationship to the real market whatsoever.

Fixed by adding `_mark_open_positions_to_market` (`app/api/orders.py`,
alongside `_stack_for`/`_execution_mode_for`, the same file's existing
per-user-stack helpers): for every open position on a user's stack, it
looks up the last price `MarketDataWorker.process_tick` cached in Redis
(`app.core.redis.get_latest_price` — already populated in production,
just never consulted here) and calls `mark_to_market` with it before the
caller reads `unrealized_pnl`. A symbol with no cached tick yet is left
alone rather than guessed at, the same "nothing to trust yet" convention
already used by `get_price_age_seconds`/`get_price_jump_pct`. `GET
/positions` and `GET /portfolio` now both call this instead of reading
`open_positions` directly, so both compute against the freshest available
price on every read — no continuous background sync required, matching
the codebase's existing preference for computing derived values at
request time from live data.

New `tests/api/test_positions.py::test_positions_and_portfolio_reflect_live_unrealized_pnl`
places a live order at 100.0, confirms `GET /positions` reports
`unrealized_pnl: 0.0` immediately after entry, then simulates a tick
arriving at 120.0 via `set_latest_price` (exactly what `MarketDataWorker`
does in production) and confirms both `GET /positions` and `GET
/portfolio` now report the correct nonzero, positive unrealized P&L.
Verified to fail against the pre-fix code (both endpoints still report
`0.0` after the price move) via `git stash`, then pass once the fix is
restored.

## Options execution never enforced the daily/weekly loss halt, position/trade caps, or the repeated-rejection circuit breaker

`evaluate_options_risk` (`app/risk/options_risk.py`) is the risk gate
`POST /options/execute` runs every strategy through. Before this round it
only ever checked `kill_switch`, `exposure_limit`, `liquidity_acceptable`,
`premium_matches_market`, `market_data_fresh`, and `broker_healthy` —
because `OptionsRiskProposal` had no fields at all for daily/weekly P&L,
open-position count, trades-today, or repeated-rejection count. Compare
that to `RiskEngine.evaluate` (`app/risk/engine.py`), the gate `POST
/orders`, `PaperTradingEngine`, and `AutoTradeSupervisor` all share: it
additionally enforces `daily_loss_limit`, `weekly_loss_limit`,
`max_open_positions`, `max_trades_per_day`, `no_repeated_rejections`, and
`no_abnormal_price_jump`. `execute_options_strategy` already builds
`stack = await _stack_for(user, db)` — the exact `_UserTradingStack` that
tracks `trades_today`/`daily_pnl`/`weekly_pnl`/`repeated_rejections`, a
few lines away from where `POST /orders` reads those same fields off it —
but never read or forwarded any of them into the proposal, because there
was nowhere to put them.

Concretely: a user whose equity trading already lost more than
`RiskLimits.max_daily_loss_pct` today gets correctly blocked from a new
`POST /orders` call by `daily_loss_limit` — but the exact same account,
in the exact same halted state, could still freely open multi-leg options
strategies through `POST /options/execute`, since that path never looked
at `daily_pnl` at all. The same gap applied to the account-wide
open-position cap, the per-day trade-count cap, and the "repeated broker
rejection" circuit breaker (blueprint §57): none of them were ever
evaluated for options, no matter how many positions were already open or
how many times the broker had just rejected a leg.

Fixed by adding `open_positions`, `trades_today`, `daily_pnl`,
`weekly_pnl`, and `repeated_rejections` to `OptionsRiskProposal`, and the
matching `daily_loss_limit`/`weekly_loss_limit`/`max_open_positions`/
`max_trades_per_day`/`no_repeated_rejections` checks to
`evaluate_options_risk`, mirroring `RiskEngine.evaluate`'s existing logic
exactly. `execute_options_strategy` now calls `stack._roll_risk_window`
(the same day/week-boundary reset `POST /orders` already does) and passes
the stack's real counters into the proposal. It also now actually
*updates* those counters after execution — `stack.trades_today += 1` and
`stack.repeated_rejections` tracking per leg placed, and
`stack.daily_pnl`/`weekly_pnl += realized_delta` when closing a leg
realizes P&L — the same updates `POST /orders`'s `place_order` already
does, which options execution had never done at all. Without that second
half, the new checks would exist but a loss or a run of rejections
produced entirely through options trading would still be invisible to
them (and to every subsequent equity order, since the counters are
shared account-wide on the same stack).

`strategy_allocation_pct`, `correlated_exposure_limit`, and
`no_abnormal_price_jump` were deliberately left out of this pass: the
first two need a well-defined "target symbol" to compute against, which a
multi-leg combination spanning several distinct option contracts doesn't
cleanly have, and the third needs a single underlying's recent price
history in the same way. Extending those to multi-leg options is a real
future improvement but a materially different problem from wiring in the
four account-wide counters this fix addresses, which needed no such
extra machinery.

New `tests/api/test_options_execute.py::test_execute_respects_the_accounts_daily_loss_limit`
places one real equity order to build the account's stack, sets
`stack.daily_pnl` to a 3% loss (past the 2% default
`max_daily_loss_pct`), then submits a bull call spread through `POST
/options/execute` and asserts it's rejected with "Daily loss" in the
response, that neither leg was ever placed as an order, and that the
persisted `RiskEvent` records `checks["daily_loss_limit"] is False`.
Verified to fail against the pre-fix code (the strategy executes
successfully despite the account-wide daily loss halt) via `git stash`,
then pass once the fix is restored.

## Maximum strategy allocation was structurally dead for paper and autonomous trading

`RiskLimits.max_strategy_allocation_pct` (blueprint §57's "Maximum strategy
allocation" — a per-strategy circuit breaker distinct from the
account-wide exposure cap) and `RiskEngine.evaluate`'s
`strategy_allocation_limit` check are both correctly implemented. But
`PaperTradingEngine.on_candle` — the single code path that builds a
`TradeRiskProposal` for both manual paper trading (`app/api/paper.py`)
and fully autonomous trading (`AutoTradeSupervisor` /
`app/workers/auto_trade_worker.py`, which drives this same engine) —
hardcoded `strategy_allocation=0.0` on every call. Since
`strategy_allocation_pct` is computed as `(proposal.strategy_allocation +
this_trade's_notional) / account_balance * 100`, a permanent `0.0` input
meant the check only ever measured *this one new trade* against the
limit, never what the strategy already had open — so a user who
tightened `max_strategy_allocation_pct` specifically to cap a single
strategy's total footprint got no protection at all: that strategy could
keep sizing up across every instrument it trades with the check silently
never firing.

This wasn't simply an unpopulated field, either. `PositionRecord`
(`app/trading/position_manager.py`) had no way to attribute an open
position back to the strategy that opened it — only `(account_id,
symbol)`. `PositionManager` is deliberately *shared* across every engine
driving the same account (see the earlier "AutoTradeSupervisor" fix in
this document, which fixed account-wide `open_positions`/`current_exposure`
by sharing one instance per user) — but that same sharing is exactly what
made per-strategy attribution impossible without a real field for it:
summing "every open position for this account" can't distinguish one
strategy's notional from another's.

Fixed by adding `PositionRecord.strategy_id: str | None`, stamped by
`PaperTradingEngine.on_candle` right when a fresh entry fills (the same
place `stop`/`target` are already attached to a new position) using
`self.strategy.name` — the same identity `TradeRiskProposal.strategy_id`
already carries. `on_candle` now computes `strategy_allocation` as the
sum of notional across every open position (any symbol, any engine)
whose `strategy_id` matches this engine's own strategy, instead of a
hardcoded `0.0`. `app/api/orders.py`'s manual/live path is intentionally
unaffected — it has no strategy concept at all (`strategy_id=None`), so
`strategy_allocation=0.0` there remains correct, not a bug.

New `tests/paper/test_engine.py::test_strategy_allocation_limit_blocks_a_second_position_for_the_same_strategy`
runs two `PaperTradingEngine`s for the *same* strategy against two
different symbols, sharing one `PositionManager` (mirroring how
`AutoTradeSupervisor` actually drives them). The first opens a position
normally; `max_strategy_allocation_pct` is then tightened to half of that
position's own notional share, so a second position for the same
strategy must fail regardless of how its own size is computed. Asserts
the position is correctly attributed (`strategy_id` matches) and that the
second entry is rejected specifically on `strategy_allocation_limit`.
Verified to fail against the pre-fix code (`PositionRecord` has no
`strategy_id` attribute at all) via `git stash`, then pass once the fix
is restored.

## Correlated exposure was never computed for paper and autonomous trading

Blueprint §85's "Correlated exposure" risk check exists to stop an account
from looking diversified while actually being one concentrated bet spread
across instruments that move together — e.g. two different Nifty
financials that are 95% correlated. `RiskLimits.max_correlated_exposure_pct`
and `RiskEngine.evaluate`'s `correlated_exposure_limit` check are both
correctly implemented, and `app/risk/portfolio.py`'s
`compute_correlated_exposure` does the real work: it looks up each open
symbol's `Instrument`, pulls its recent candle history, builds a
correlation matrix against the target symbol, and returns the notional
that should count against the limit. `app/api/orders.py`'s live order
path calls this function on every order and passes the result into
`TradeRiskProposal.correlated_exposure`.

`PaperTradingEngine.on_candle` — the single code path that builds a
`TradeRiskProposal` for both manual paper trading (`app/api/paper.py`)
and fully autonomous trading (`AutoTradeSupervisor` /
`app/workers/auto_trade_worker.py`, which drives this same engine) never
called `compute_correlated_exposure` at all. `TradeRiskProposal`'s
`correlated_exposure` field simply kept its dataclass default of `0.0` on
every call, so `correlated_exposure_limit` could never fail for paper or
autonomous trading no matter how concentrated an account actually was in
correlated instruments — the check was correctly implemented and
completely unreachable outside the one live-order code path.

`compute_correlated_exposure` needs a database session to look up
instruments and candle history, but `on_candle` previously took no
session at all — it's driven directly from in-memory candles in both call
sites, and the wide unit test suite in `tests/paper/test_engine.py`
exercises it with plain `Candle` objects and no database. Fixed by adding
an optional `db: AsyncSession | None = None` parameter to `on_candle`,
threaded through from its two production callers (`app/api/paper.py`'s
`feed_candle`, which already has a request-scoped session, and
`app/workers/auto_trade_worker.py`'s `_process`, which already has one
too). When a session is available, `on_candle` computes
`other_position_notionals` from every other open position in the shared
`PositionManager` and calls `compute_correlated_exposure` exactly the way
`app/api/orders.py` already does, passing the result into the proposal.
When `db` is `None` — every existing DB-free unit test — it falls back to
`0.0`, the same "no correlation data available yet" convention already
used elsewhere in this engine (`market_data_age_seconds`,
`recent_price_jump_pct`). `compute_correlated_exposure` itself already
degrades gracefully (partial or zero data) for a symbol with no
registered instrument or insufficient candle history, so a caller that
does pass a session never risks a hard failure over incomplete market
data.

New `tests/paper/test_engine.py::test_correlated_exposure_limit_blocks_a_position_in_a_correlated_instrument`
runs two `PaperTradingEngine`s on two different symbols with identical
close-price history (correlation of 1.0, comfortably above a 0.5
threshold), sharing one `PositionManager`. The first engine opens a
position normally; `max_correlated_exposure_pct` is then tightened to
half of that position's own notional share of the account — a threshold
the already-open position alone must already exceed, regardless of how
`RiskEngine.evaluate` internally sizes the second engine's own candidate
trade (it recomputes size internally via `RiskLimits.risk_per_trade_pct`,
not the strategy's own `risk.risk_percent`, so the two can differ; sizing
the threshold off the already-open position's own notional keeps the test
correct either way). The second engine's matching entry signal is then
asserted to be rejected specifically on `correlated_exposure_limit`, with
no second position opened. Verified to fail against the pre-fix code (the
new test calls `on_candle(candle, db)`, which raises against the
one-argument pre-fix signature) via `git stash`, then pass once the fix
is restored.

## Market-entry signals could emit a long whose stop sat above the entry

`StrategyEngine._resolve_entry_and_stop` handles three entry types. The two
retest branches (`fvg_retest`, `order_block_retest`) are structurally safe:
they anchor the stop *beyond* the far edge of the zone they enter from, so
the stop is always on the losing side of the entry by construction. The
default `market` entry type — which is `EntryConfig`'s default, so it is
what any strategy that doesn't name an entry type gets — is not:

```python
entry = context.current_price
stop = smc.dealing_range.range_low if is_bullish else smc.dealing_range.range_high
```

`dealing_range` comes from `app/smc/premium_discount.py`, and it is simply
*the most recent confirmed swing high and the most recent confirmed swing
low*. Neither is guaranteed to bracket the current price. Once price drifts
below the last confirmed swing low, a bullish strategy resolves to
`entry=current_price, stop=range_low` with the **stop above the entry** —
an inverted bracket. The mirror case applies to a short once price rises
above the last swing high.

Nothing downstream could catch this. `StrategyEngine.evaluate`'s only guard
on the resolved pair rejected `entry == stop` but never checked which
*side* the stop was on. `RiskEngine.evaluate` is equally blind: it measures
the stop as `risk_per_unit = abs(proposal.entry - proposal.stop)`, and
`abs()` erases the sign — `TradeRiskProposal` carries no `direction` field
to compare against in the first place, so an inverted bracket passes
`valid_stop_distance` and every other risk check cleanly.

The consequence is not merely a malformed signal. Both exit paths —
`BacktestEngine._check_exit` and `PaperTradingEngine._maybe_exit` — test
`candle.low <= position.stop` for a long. With the stop above the entry
that is *trivially true on the very next candle*, so the position closes
immediately, filled at the stop, i.e. **above** where it was entered. The
trade is booked as a guaranteed profit whose `exit_reason` is
`stop_loss`. Reproduced against the real engine on a gentle decline (each
step well inside `RiskLimits.max_price_jump_pct`, so the abnormal-jump
check does not intervene — a violent crash actually *is* caught by that
check, which is why this surfaces during ordinary drift rather than a
crash):

```
[23] OPEN entry= 96.00 stop= 97.46  <-- stop ABOVE entry
[24] CLOSE reason=stop_loss price= 97.46 pnl=+1000.00
[25] OPEN entry= 94.00 stop= 97.46
[26] CLOSE reason=stop_loss price= 97.46 pnl=+1000.00
```

Four fabricated +1R "stop losses" in a row, turning a steady losing drift
into `daily_pnl +4000`. That fake profit is not inert: it feeds
`TradeRiskProposal.daily_pnl` and therefore *relaxes* the
`daily_loss_limit` check, pushing the loss halt further out on precisely
the day it is needed most. The same fiction reaches `Trade` rows,
persisted positions, the SL_HIT notification (blueprint §63) reporting a
stop loss that made money, `Signal` rows from the scanner (blueprint §27
defines the signal contract as a `LONG` with `stop` strictly below
`entry`), and backtest reports — where a long-only strategy measured
across a sustained decline is scored with an inflated win rate and profit
factor, i.e. the metrics flatter the strategy exactly where it is worst.

Fixed with a direction-aware invariant in `StrategyEngine.evaluate`,
placed immediately after the existing degenerate-stop guard so it covers
every entry type and every consumer at once: if the stop is not on the
losing side of the entry, the evaluation returns `matched=False` with
`stop_on_wrong_side_of_entry` in `missing`, and no `entry`/`stop` is
emitted at all. Suppressing the signal (rather than clamping the stop) is
the correct answer here because an inverted bracket means the setup's own
premise no longer holds — a bullish market entry below the dealing range
is not a discounted long, it is a strategy whose range has been broken.

`tests/strategy/test_engine.py::test_market_entry_below_the_dealing_range_low_does_not_emit_an_inverted_long`
builds a zigzag that establishes a confirmed swing high and low, then
drifts price steadily below that swing low, and asserts the evaluation is
rejected with `stop_on_wrong_side_of_entry` and emits no bracket. Verified
to fail against the pre-fix code (which returned `matched=True,
entry=85, stop=97.857`) via `git stash`, then pass once the fix is
restored. A companion test,
`test_market_entry_inside_the_dealing_range_still_emits_a_valid_long`,
pins the ordinary case — price above the range low still produces a
well-formed `stop < entry < target` signal — so the guard cannot silently
over-block legitimate setups.

## Equal-highs/lows liquidity pools were swept by their own constituent swings

Blueprint §22's Liquidity Engine specifies a strict ordering — liquidity
pool forms, *then* price sweeps it, *then* rejection, *then* structure
confirmation. `detect_equal_levels` broke the first link:

```python
avg_price = sum(s.price for s in group) / len(group)
first = min(group, key=lambda s: s.index)
pools.append(LiquidityPool(..., price=avg_price, formed_index=first.index, ...))
```

An equal-highs pool does not exist until the swing that makes the level
"equal" has printed — that is, until its **last** member. Anchoring
`formed_index` at the **first** member left `detect_sweeps` scanning from
`formed_index + 1`, a window that still contained the pool's own later
members. Since `price` is the group *average*, any member priced beyond
that average trips the sweep test on its own candle. And because a swing
high by construction closes below its own high, `rejected` (set from
`candle.close < pool.price`) is almost always true too — so the phantom
event is reported as a rejection, the highest-conviction variant of the
pattern.

The engine therefore announced "buy-side liquidity at X was swept, with
rejection" at the exact moment a double top completed, when price had
never traded through the level at all. Reproduced on a NIFTY-scale double
top (swing highs 22,000 and 22,008, pool price 22,004): the pool is
reported `swept=True, swept_index=40, rejected=True` while the highest
high in the entire series is 22,008 — the equal high itself, never
exceeded.

The same line causes a second, opposite failure. `detect_sweeps` `break`s
on its first hit, so the phantom sweep permanently pins `swept_index` and
the *genuine* sweep, when it eventually arrives, is never recorded. The
detector thus both invents a sweep that did not happen and goes blind to
the one that did.

Nothing downstream filters this. `SMCContext.recent_sweeps` simply returns
pools with `p.swept`, and `app/strategy/evaluator.py`'s
`ConditionType.LIQUIDITY_SWEEP` only checks `current_index -
p.swept_index < condition.lookback`. The blueprint's own shipped example
strategy ("Bullish Liquidity Sweep", §34) is built on precisely this
condition, so a phantom sweep flows straight through `StrategyEngine` into
`PaperTradingEngine`/`AutoTradeSupervisor` and becomes a real order.

The repository's own test fixture had been documenting the bug without
catching it. `tests/smc/test_liquidity.py`'s `EQUAL_HIGHS` fixture
comments index 8 as "sweeps above both highs, closes back below ->
rejection", but the engine was actually recording `swept_index=5` — the
pool's own second equal high. `test_sweep_and_rejection_detected`
asserted only the booleans `swept is True` / `rejected is True`, so it
passed green against the wrong candle.

Fixed by anchoring each pool at its last member (`max(group, key=index)`)
rather than its first, so `detect_sweeps` structurally cannot see any
member candle and the first genuine post-formation penetration is what
gets recorded. `member_indices` is now sorted for stable ordering.
`detect_session_levels` already got this right — its docstring is explicit
that a level is anchored at the first candle of the *following* period,
"since that is when the level becomes a resting liquidity target rather
than an in-progress extreme" — so this change brings equal-level pools in
line with the convention the module already documented.

`test_sweep_and_rejection_detected` is tightened to assert
`swept_index == 8` and that the sweep index is not one of the pool's own
members, so it can no longer pass on a self-sweep. Two new tests cover the
invariants directly:
`test_pool_is_not_swept_by_its_own_member_swing` builds a double top where
price never takes out either high and asserts no sweep is reported at all,
and `test_pool_is_anchored_at_its_last_member` pins the anchoring rule.
All three were verified to fail against the pre-fix code via `git stash`
and pass once the fix is restored.

One related question is deliberately left alone here: `price` is the
*average* of the group's members, so even with correct anchoring a candle
that trades above the average but below the highest equal high counts as a
sweep, where SMC convention places resting liquidity above the highest of
the equal highs. That is a behavioural change to the detector rather than
a fix to this defect, and is better evaluated on its own.

## ScannerWorker ignored each strategy's declared timeframe

`StrategyDefinition.timeframe` is a first-class field of the DSL — blueprint
§34's example strategy carries `"timeframe": "15m"` as a property of the
*strategy*, not of whatever happens to be scanning it. Every other consumer
honours it: `BacktestEngine` and `PaperTradingEngine` both build their
`EvaluationContext` with `self.strategy.timeframe`, and
`AutoTradeSupervisor._process` loads candles with
`get_candles(db, instrument.id, strategy.timeframe)` — the supervisor keeps
its own `self.timeframe` attribute purely for a startup log line, which is
itself evidence of the intended contract.

`ScannerWorker.run_once` was the sole outlier. It loaded exactly one candle
series per instrument, for the worker's own `self.timeframe` (wired as
`"15m"` in `app/workers/main.py`), analyzed it once, and then evaluated
**every** active strategy against that single context — the strategy query
has no timeframe filter, and `strategy.timeframe` was never read anywhere
in the file. A strategy the user declared on `1h` was therefore matched
against 15m structure.

The consequences compound. `StrategyEngine._resolve_entry_and_stop` derives
the entry from `context.current_price` and the stop from the 15m dealing
range, so the published levels belong to a timeframe the strategy never
asked for — and a 15m dealing range is materially tighter than a 1h one.
Since `calculate_position_size` is `risk_amount / abs(entry - stop)`, a user
acting on that signal takes a position several times larger than the
strategy was designed for, with every downstream notional check computed
from the same undersized stop distance. The persisted `Signal.timeframe`
reads `"15m"` for a `1h` strategy, and `GET /signals` reports it that way.
Worse, `AutoTradeSupervisor` reloads the *correct* `1h` series for that same
strategy row, so the signal the user was alerted about and the trade the
autonomous supervisor actually evaluates come from two different candle
series, with nothing reconciling them.

The event-type conditions are distorted rather than merely rescaled:
`bos`/`mss`/`choch`/`liquidity_sweep` filter on
`current_index - event.index < condition.lookback`, so a `lookback=5` on a
1h strategy means five hours, but evaluated on 15m candles it silently
becomes 75 minutes — a different filter, not a scaled one. And for a
strategy declared on a timeframe this deployment never derives at all
(`derived_timeframes=["5m", "15m"]`), the worker did not report missing
data; it quietly substituted 15m and emitted a confident signal.

Fixed by building contexts lazily and caching them per timeframe, keyed off
each strategy's own `strategy.timeframe`. The scan timeframe is still always
analyzed (so the `setups` journal, which records raw per-timeframe structure
detections independent of any strategy, keeps behaving exactly as before),
and the "analyze once per instrument" saving survives for the common case
where every strategy shares the scan timeframe. When a strategy's own
timeframe has no usable history the strategy is **skipped**, never evaluated
against a substitute series — a signal computed off the wrong candles is
worse than no signal. `Signal.timeframe` already read `context.timeframe`,
so it became correct automatically.

Every pre-existing scanner test paired `ScannerWorker(timeframe="15m")` with
strategy fixtures hard-coding `"timeframe": "15m"`, so the substitution was
a no-op in all of them and none could have caught this. The new
`tests/workers/test_scanner_worker.py::test_scanner_uses_each_strategys_own_timeframe`
registers a `1h` strategy against an instrument whose 15m series is
deliberately flat (no FVG, never matches) while only its 1h series contains
the setup. It asserts first that with no 1h history the strategy produces no
signal at all — rather than being evaluated against 15m — and then, once 1h
candles exist, that exactly one signal is emitted and persisted with
`timeframe == "1h"`. Verified to fail against the pre-fix code via
`git stash`, then pass once the fix is restored.

Two related observations are deliberately left out of this change.
`strategy.market` is ignored by the same query, so a strategy declared for
one market is still evaluated against every active instrument; that is a
real scoping question but a broader behavioural change than this fix.
Separately, `compute_correlated_exposure` correlates return series by list
position rather than by timestamp, so two instruments with different candle
coverage produce a meaningless correlation — worth its own investigation.

## Correlation was computed by list position instead of by timestamp

Blueprint §85's correlated-exposure check exists to notice when an account
that looks diversified is really one concentrated bet. The engine
(`app/risk/correlation.py`) computed each instrument's close-to-close
returns independently and then correlated them with:

```python
n = min(len(a), len(b))
a, b = a[-n:], b[-n:]
```

That aligns two series by *list position from the tail*, which silently
assumes both instruments printed the same number of bars over the same
wall-clock span. They routinely do not. An illiquid instrument prints fewer
bars over the same period; an instrument listed later simply starts later;
ingestion gaps drop bars. In every such case the last N returns of one
symbol cover a different time range than the last N of the other, and the
two get correlated as though they were contemporaneous.

Demonstrated against the real functions with two symbols following the
**identical price path at identical timestamps** — genuinely correlated
1.0 — differing only in that the second is illiquid and prints every other
bar:

```
A: 119 returns spanning 09:15-15:00
B:  59 returns spanning 09:15-14:45
pearson uses n=min=59: A[-59:] vs B[-59:]
  A[-59:] actually covers 00:30-15:00
  B[-59:] actually covers 09:45-14:45
correlation reported:                    -0.789
correlation on timestamp-aligned bars:    1.0
```

A perfect positive correlation is reported as strongly *negative*, because
the choppy first half of one series is being lined up against the trending
second half of the other. The check uses `abs(corr) >= threshold`
(default 0.7), so the error runs in both directions: genuinely correlated
positions can fall below the threshold and escape the limit, while
genuinely unrelated instruments can be flagged and block a legitimate
trade. The number simply carries no information about the instruments'
actual relationship.

Fixed by making alignment structural rather than incidental.
`build_correlation_matrix` now takes closes keyed by timestamp
(`dict[str, dict[datetime, float]]`) instead of bare return lists, and for
each pair intersects on the timestamps both symbols actually have, sorts
them, and computes returns over that shared series — so every pair of
points being correlated spans the same interval. A pair with fewer than
three shared bars yields no correlation at all rather than a fabricated
one, which `correlated_exposure` already treats as "uncorrelated, no
evidence". `closes_by_timestamp` is the small helper that builds that
input, and `compute_correlated_exposure` in `app/risk/portfolio.py` now
feeds it. `pearson_correlation` keeps its list API and its tail-truncation
as a defensive fallback, with its docstring now stating plainly that
aligning the series is the caller's job.

`tests/risk/test_correlation.py::test_correlation_aligns_series_on_shared_timestamps`
builds exactly the liquid/illiquid pair above and asserts the matrix
reports 1.0. It also pins the *old* behaviour's harm directly — computing
the two return series and correlating them by position must give a
strongly negative number — so the test cannot later degrade into merely
passing because the API changed shape.
`test_build_correlation_matrix_skips_pairs_with_too_little_overlap` covers
the barely-overlapping case. The pre-existing
`test_build_correlation_matrix_covers_every_pair` was migrated to the new
timestamp-keyed input while keeping its original intent (a pair whose
returns are an exact 2x multiple still correlates 1.0).

## The strategy-level kill switch could never stop a strategy

Blueprint §58 requires three kill-switch levels: global, account, and
strategy. The global and account levels work — both the admin endpoint and
the engine key them on `str(user.id)`. The strategy level was a silent
no-op end to end, because the two sides used different identifiers for
"which strategy".

`POST /admin/kill-switch/strategy/{strategy_id}` writes
`kill:strategy:<strategy_id>`, and `strategy_id` there is the
`strategies.id` UUID — the only strategy identifier the platform exposes
anywhere. It is what `GET /strategies` returns, and
`Signal.strategy_id`, `Order.strategy_id` and `Trade.strategy_id` are all
foreign keys to it. Meanwhile `PaperTradingEngine.on_candle` asked Redis
for `kill:strategy:<StrategyDefinition.name>` — the free-text DSL name —
because the engine was only ever handed the parsed `StrategyDefinition`,
which has no id field at all. The keys never matched.

Reproduced against live Redis, arming the kill before the first candle:

```
=== admin kills by the real Strategy.id UUID (what the API exposes) ===
   kill key: kill:strategy:68c111a8-1a4d-424d-b45e-f3140ea1bd19
   -> order_created=True   rejection=None                                trades_today=1
=== same kill, but keyed by StrategyDefinition.name ===
   kill key: kill:strategy:Bullish FVG retest
   -> order_created=False  rejection=Strategy Bullish FVG retest is stopped  trades_today=0
```

An admin stopping a runaway strategy the only way the API allows watched
it keep placing orders. `app/api/orders.py` and `app/api/options.py` both
pass `strategy_id=None` (a manual order has no strategy), which is
correct — so `PaperTradingEngine`, driving both manual paper sessions and
`AutoTradeSupervisor`, was the *only* path where the strategy level
applied at all, and there it never fired.

The DSL name was never a viable key even for an admin who typed the name
instead of the id. `strategies.name` has no uniqueness constraint, so two
users' identically-named strategies would share one kill key — a kill
crossing account boundaries. And `PUT /strategies/{id}` rewrites
`row.name`, after which `AutoTradeSupervisor` swaps `engine.strategy` in
place, so a name-keyed kill would stop applying the moment a strategy was
renamed.

Fixed by giving `PaperTradingEngine` the real identity instead of deriving
one from the mutable DSL name: a `strategy_id: str | None = None`
constructor argument, passed as `str(strategy_row.id)` by both production
callers (`app/api/paper.py`'s `create_paper_session` and
`AutoTradeSupervisor._process`), both of which already had the row in
hand. `load_kill_switch_state` and `KillSwitchState.is_blocked` already
treat `None` as "no strategy level to check", so a bare engine with no
database row behind it degrades honestly rather than matching on a name.

The same root cause reached one more place. The per-strategy allocation
attribution added for `max_strategy_allocation_pct` (see the
"Maximum strategy allocation" section above) stamped
`PositionRecord.strategy_id` with `self.strategy.name` and matched on it,
so renaming a strategy silently orphaned its already-open positions from
their own allocation limit on the very next candle. Both the stamp and the
match now use the same stable `strategies.id`, and the match additionally
requires `self.strategy_id is not None` so two id-less engines are never
conflated by `None == None`.

Why the existing tests missed it, in all three places that looked like
coverage: `tests/api/test_admin.py` round-trips an *opaque* string through
the endpoint and asserts only `is_strategy_killed(that_string)` — any key
naming passes. `tests/risk/test_engine.py` kills `"strat-1"` and evaluates
a proposal whose `strategy_id` is also `"strat-1"`, so the substitution
under test is a no-op. And `tests/paper/test_engine.py`'s kill-switch test
exercises the real Redis wiring but only via `set_global_kill()` — the one
level that was never broken. The new
`test_paper_engine_respects_strategy_level_kill_switch` arms the kill
through `set_strategy_kill(<the id the engine was given>)`, asserts
`risk_failed_check == "kill_switch"` with zero trades, and then asserts
the converse: a kill on one strategy's id must *not* stop an engine
running a different one.

## GET /portfolio reported zero realized P&L for every closed trade

`GET /portfolio` built its response from two different sources, and they
disagreed about what "the portfolio" is:

```python
positions = await _mark_open_positions_to_market(stack, str(user.id))
...
total_unrealized_pnl=sum(p.unrealized_pnl for p in positions),
total_realized_pnl=sum(p.realized_pnl for p in positions),
total_exposure=exposure.total_exposure,
```

`total_exposure` came from the persisted `positions` table via
`compute_portfolio_exposure`, but `total_realized_pnl` was summed over
`positions` — which is `PositionManager.open_positions()`, filtered to
records where `is_open` (`quantity != 0`). Realized P&L exists precisely
*because* a position closed, so restricting it to open positions is a
contradiction in terms.

Reproduced directly against `PositionManager`, opening 50 @ 100 and
closing at 120 in two steps:

```
after OPEN 50 @100                          -> total_realized_pnl=0.0    (truth 0.0)
after PARTIAL close 20 @120 (realized 400)  -> total_realized_pnl=400.0  (truth 400.0)
after FULL close (realized 1000 total)      -> total_realized_pnl=0      (truth 1000.0)
```

The number is non-zero only during the transient window where a position
is *partially* closed — the one state where it is least meaningful — and
realizing **more** profit drives the reported figure back down to zero. End
to end through the API, a user who opened 100 shares at 100 and closed them
at 110 sees `total_realized_pnl: 0.0` while the `trades` journal row and
the persisted `positions` row both record a ₹1000 gain.

Nothing evicts the closed record either: `PositionManager.close()` has no
call sites anywhere in the codebase, so the in-memory `PositionRecord`
retains its correct `realized_pnl` indefinitely — it is simply filtered out
by `is_open` before the sum. The data was right; the query over it was
wrong.

Fixed by sourcing realized P&L from the same place `total_exposure` in the
same response already comes from. `compute_portfolio_exposure` now also
returns `total_realized_pnl`, summed with `func.coalesce(func.sum(...), 0)`
over every `positions` row for that `(user_id, execution_mode)` **without**
the `is_open` filter, and `GET /portfolio` reads that. Besides being
correct, the persisted rows survive a process restart, which the in-memory
manager does not — `_STACKS` is process-local.

`total_unrealized_pnl` deliberately stays restricted to open positions: a
closed position has nothing left to mark, and `mark_to_market` returns
early on a flat position, so a closed record's `unrealized_pnl` is stale
rather than zero. Including closed records there would reintroduce the
mirror-image bug.

Blueprint §60 lists realized P&L as a Position Manager responsibility and
§86 treats it as a portfolio-level figure; §93/§104's dashboard reads it.

Both pre-existing tests in `tests/api/test_portfolio.py` and
`tests/api/test_positions.py` call `GET /portfolio` and both pass, but
neither ever closes a position and neither asserts anything about
`total_realized_pnl` — the closest is an assertion on
`total_unrealized_pnl` for a still-open position. The new
`test_portfolio_reports_realized_pnl_after_a_position_closes` opens a
position through `POST /orders`, closes the full quantity at a profit,
confirms the persisted row is flat with a positive `realized_pnl`, and
asserts the endpoint reports that same figure. Verified to fail against the
pre-fix code with `assert 0.0 == 1000.0`.

## A partial close journaled the whole position as the closed quantity

Both `POST /orders` and `POST /options/execute` write a `trades` row
(blueprint §61's journal) whenever a fill realizes P&L:

```python
realized_delta = position_after.realized_pnl - realized_pnl_before
if realized_delta != 0 and position_before is not None:
    await record_trade(
        ...
        quantity=abs(position_before["quantity"]),
        pnl=realized_delta,
```

That condition is any non-zero realized delta, which includes a **partial**
reduce — but the quantity passed is the whole pre-fill position. Closing 40
of a 100-unit position therefore journaled `quantity=100` alongside a `pnl`
covering only those 40 units, so the row did not agree with itself:

```
PARTIAL CLOSE of 40 out of 100 @ 100 -> 120
  what record_trade is passed: quantity=100.0  entry=100.0  pnl=800.0
  implied by those numbers:    100 units x (120-100) = 2000
  actually closed:             40 units x (120-100) = 800   <-- matches pnl
```

It also double-counts. The later close of the remaining 60 journals another
row saying 60, so `SUM(trades.quantity)` across the position reaches 160 for
what was only ever a 100-unit position — any per-instrument volume or
average-size figure computed off the journal is inflated.

Fixed by passing `min(final_order.filled_quantity, abs(position_before["quantity"]))`
at both call sites. The `min` is what makes this correct for a flip as well
as a reduce, which is worth spelling out because the three cases differ:

| case | pre-fill | fill | realized P&L covers | old | new |
|---|---|---|---|---|---|
| partial reduce | 100 long | sell 40 | 40 units | 100 ✗ | 40 ✓ |
| full close | 100 long | sell 100 | 100 units | 100 ✓ | 100 ✓ |
| flip | 100 long | sell 120 | 100 units | 100 ✓ | 100 ✓ |

On a flip the fill exceeds the position: 100 units close and a fresh 20-unit
short opens, and the realized P&L covers only the 100 that closed — so
clamping to the pre-fill size, not taking the fill quantity outright, is
what keeps that row honest. The old code was right in two of the three
cases, which is why this went unnoticed.

Nothing reads the `trades` table through the API today — there is no
`GET /trades` — so this corrupted the journal rather than producing a wrong
response, which is exactly why it needed catching before anything starts
reading it.

`tests/api/test_orders.py::test_partial_close_journals_only_the_quantity_actually_closed`
opens a position, closes part of it, and asserts both that the journaled
quantity matches the fill and that the row reproduces its own `pnl` from
`quantity * (exit - entry)` — the internal-consistency check that the
old row failed. Verified to fail against the pre-fix code with
`assert 100.0 == 25.0`.

Producing a partial close through the API is less obvious than it looks:
`PlaceOrderRequest` has **no `quantity` field**. Every order is sized by the
server as `balance * risk_per_trade_pct / abs(entry - stop)`, so a client
cannot ask for a specific size, and a `quantity` key in the request body is
silently ignored. A partial reduce is produced by giving the closing order a
*wider* stop than the opening one, which buys proportionally fewer units.
The realized-P&L test added alongside the previous section originally passed
a `quantity` field for its full close; it happened to work because the
equal-width stop produced an equal size, but it implied a contract the API
does not have, and has been rewritten to say what actually drives the sizing.

## Autonomous trading enforced the daily caps per engine, not per account

Blueprint §57 lists *maximum daily loss*, *maximum weekly loss*, *maximum
trades per day* and *repeated order rejection* as user-configurable risk
controls. They are stored on the `User` row by `POST /auto-trading/enable`
— one set per **account**. `AutoTradeSupervisor` enforced them per
**engine**, and it runs one `PaperTradingEngine` per
`(user, strategy, instrument)` triple.

The counters lived on the engine as plain instance attributes:

```python
self.trades_today = 0
self.daily_pnl = 0.0
self.weekly_pnl = 0.0
self.repeated_rejections = 0
```

This is the same defect the shared `PositionManager` already fixed for
`max_open_positions` — and the comment introducing that shared instance
says so explicitly, that "a user's `max_open_positions`/exposure limits are
account-wide across every instrument and strategy they're auto-trading."
The position ledger was shared; these four counters were left behind on the
engine, so the reasoning was applied to one half of §57 and not the other.

Reproduced with two engines for one account, sharing a `PositionManager`
exactly as the supervisor wires them, under `max_trades_per_day=1` (with
`max_open_positions=5` so it could not be the binding constraint):

```
  entry opened on INSTA
  entry opened on INSTB
max_trades_per_day = 1
entries actually opened same day: 2
  engine INSTA: trades_today=1
  engine INSTB: trades_today=1
```

Two entries under a cap of one, because each engine evaluated its own
`0 < 1`. Generalised, a user trading N instruments across M strategies gets
up to **N×M × `max_trades_per_day`** autonomous entries per day, and
`run_once` iterates every `Instrument.active`, so N is the whole instrument
universe rather than a hand-picked pair.

The loss limits are the dangerous half. `daily_loss_limit` compares
`daily_pnl` against the configured percentage, so with the counter split
per triple the halt only fires once a **single** `(strategy, instrument)`
pair has lost the entire configured limit by itself. An account can lose a
multiple of its own daily cap before anything stops it — on the one path
that runs unattended with no human watching. `repeated_rejections` splits
the same way, so §57's repeated-rejection breaker needs N×M consecutive
broker rejections instead of the configured threshold.

Fixed by extracting the four counters, plus the day/week window
bookkeeping and the roll logic, into a small `RiskWindow` dataclass, and
giving `PaperTradingEngine` a `risk_window` constructor argument that
defaults to a private instance — so a standalone engine behaves exactly as
before. `AutoTradeSupervisor` now keeps `self._risk_windows: dict[str,
RiskWindow]` alongside `self._position_managers` and passes the same
instance to every engine for a user. The engine keeps `trades_today`,
`daily_pnl`, `weekly_pnl` and `repeated_rejections` as properties
delegating to that window, so existing readers (`app/api/paper.py`'s state
response, the tests) and the `+=` updates inside `on_candle` are unchanged.

Each engine still has its own `MockBroker` with its own balance, which is
fine for the percentage limits: the shared `daily_pnl` is now the account's
true total loss, and every engine's balance is the same configured starting
balance, so `daily_loss_pct` is computed against the account size the user
actually configured.

Why the existing tests missed it, in both places that looked like coverage:
`test_supervisor_caps_open_positions_account_wide_across_instruments`
builds exactly the two-instrument fixture this needs but varies only
`max_open_positions` and asserts only on positions.
`test_supervisor_notifies_daily_loss_limit_distinctly` monkeypatches
`RiskEngine.evaluate` to return a hard-coded failing `daily_loss_limit`
check and asserts only the resulting notification type — the real counter
never participates, so the substitution under test is a no-op for this bug.
The new `test_supervisor_caps_trades_per_day_account_wide_across_instruments`
reuses that same fixture with `max_trades_per_day=1` and
`max_open_positions=5`, and asserts one position account-wide, a rejected
`RiskEvent` naming `max_trades_per_day`, and that exactly one shared
`RiskWindow` exists holding `trades_today == 1`. Verified to fail against
the pre-fix code with `assert 2 == 1`.

## A closed autonomous trade was journaled against the wrong strategy

`PositionManager` is shared per **user** and keyed only by `(account_id,
symbol)`, but `AutoTradeSupervisor` runs one `PaperTradingEngine` per
`(user, strategy, instrument)` triple. `PaperTradingEngine.on_candle` calls
`_maybe_exit` on whatever position exists for its `(account_id, symbol)`
without ever checking who opened it:

```python
position = self.position_manager.get(self.account_id, self.symbol)
if position is not None and position.is_open:
    closed_pnl, exit_reason, exit_price = await self._maybe_exit(position, candle)
```

So with two eligible strategies on one instrument, whichever is iterated
first reaches the shared position and books the exit for a position the
*other* strategy opened. The journaling site then recorded that trade
against itself:

```python
strategy_id=strategy_row.id,
strategy_version=strategy_row.version,
```

Reproduced with one user, one instrument, and two eligible strategies —
`NEVER-MATCH` (version 7, inserted first, a bearish order-block DSL that
cannot fire on this data) and `WORKING` (version 3, the bullish FVG fixture
that does):

```
journaled against : NEVER-MATCH   strategy_version=7
journal.strategy  : NEVER-MATCH
opened_at=09:24  closed_at=09:24  identical=True
pnl=181.82  (fill numbers themselves are genuine)
```

Four wrong outputs for one trade. The `strategy_id`, `strategy_version` and
`journal.strategy` all name a strategy that produced no signal at all —
`GET /strategies/{id}/versions/7` resolves to a DSL that could not have
produced this trade. And `opened_at` collapses onto the closing candle,
because `self._opened_at` is keyed by the *opener's* triple, so the
observing engine's `pop` misses and falls back to `latest.timestamp`; every
holding-period figure read off that row is zero. Entry, exit, quantity,
stop, target and P&L are all correct — the position snapshot is genuine.
Only the identity is wrong.

This is the same partially-applied-fix shape as the section above: the
`PositionManager` and then the `RiskWindow` were promoted to per-user, but
the exit and journaling path still assumed one strategy per position. The
correct owner was already being recorded — `PaperTradingEngine` stamps
`new_position.strategy_id = self.strategy_id` when an entry fills, added so
`max_strategy_allocation_pct` could attribute notional — the journaling
site simply never consulted it.

Fixed by capturing `position_before.strategy_id` as the owner and, when it
differs from the observing engine's strategy, loading that `StrategyRow`
for the `strategy_id`, `strategy_version` and journal/notification name,
and popping `_opened_at` under the *owner's* triple so the real entry
timestamp is used. A `None` owner (a position predating this attribution,
or from a path that does not stamp it) still falls back to the observing
engine, which is the best answer available.

Deliberately **not** fixed by having a non-owning engine skip the position
entirely. That would also correct the attribution, but it changes *when* a
position closes: if the owning strategy is later deactivated or made
ineligible while its position is open, no engine would ever close it and
the position would be orphaned. Attributing correctly leaves the existing
"whoever sees it closes it" behaviour intact and only fixes the identity —
a smaller blast radius on the one path that runs unattended.

Every one of the twelve pre-existing tests in
`tests/workers/test_auto_trade_worker.py` creates exactly **one**
`StrategyRow` per user, so `assert trades[0].strategy_id == strategy_id`
held no matter what the code did — both sides were the same value. The
`strategy` half of the engine key had no coverage at all, even though
`run_once` iterates every eligible strategy against every active
instrument, which makes several strategies sharing an instrument the normal
configuration rather than an edge case. The new
`test_supervisor_journals_a_close_against_the_strategy_that_opened_it`
registers both strategies, asserts the trade is journaled against `WORKING`
at version 3 (and explicitly *not* against `NEVER-MATCH`), and asserts
`opened_at < closed_at`. Verified to fail against the pre-fix code on the
strategy id.

## Paper/auto-trade position sizing ignored the account risk limit (§57)

`PaperTradingEngine.on_candle` — the engine behind both `POST
/paper/{session}/candle` and every autonomous trade `AutoTradeSupervisor`
places — built its `TradeRiskProposal` without a `proposed_quantity`.
`RiskEngine.evaluate` sizes the proposal itself in that case, from
`RiskLimits.risk_per_trade_pct`, and every downstream check
(`exposure_limit`, `strategy_allocation_limit`, `correlated_exposure_limit`)
is computed from `quantity * entry`. The engine then turned around and
sized the order it actually submitted from a *different* number: the
strategy DSL's own `StrategyDefinition.risk.risk_percent`.

The two never had to agree, and by default they don't. `RiskLimits.
risk_per_trade_pct` defaults to 0.5% and is what `POST
/auto-trading/enable` writes per account; `RiskConfig.risk_percent`
defaults to 0.5 but is validated only as `gt=0, le=100`, so any strategy —
including one the AI strategy builder proposes — may carry a much larger
number. Reproduced directly against the engine with a 100,000 balance, the
0.5% account limit, and a strategy asking for 5.0%:

```
RiskEngine approved qty : 151.5152  notional 15606.06
actually filled qty     : 1515.1515  notional 165151.52
ratio                   : 10.00
risk at stop (approved) : 0.500% of balance
risk at stop (filled)   : 5.000% of balance
exposure after fill     : 165.15% vs max_exposure_pct 50.0%
```

Ten times the approved quantity, ten times the risk per trade the user
configured, and 165% account exposure through a `max_exposure_pct` of 50%
that had been checked against the 15.6% version. The account-level limit
was not merely loose here — it was inert, on the one path that runs
unattended.

The same split does harm in the other direction. A strategy deliberately
risking *less* than the account cap had every exposure check computed
against a position several times larger than the one it would take, so
trades sitting comfortably inside the user's configured limits were
rejected.

The fix sizes the trade once, before the proposal, from `min(strategy
risk_percent, limits.risk_per_trade_pct)` — the account value is a cap, so
it bounds the strategy's request rather than replacing it — and passes that
exact quantity as `TradeRiskProposal.proposed_quantity`. The field already
existed for precisely this ("if None, engine sizes the position"); it was
simply never used by this engine. The notional every check is computed from
is now the notional that gets filled. The live path (`app/api/orders.py`)
never had the split — it sizes from `limits.risk_per_trade_pct` on both
sides — and `app/backtest/engine.py` is deliberately left alone: it has no
`RiskLimits` at all, so the strategy's own percent is the only authority
there and the two sides cannot disagree.

Worth recording honestly: this split was noticed during the earlier
`strategy_allocation` work, written off as a quirk, and a test threshold
was calibrated around the inflated quantity it produces
(`first_notional_pct * 0.5` in `tests/paper/test_engine.py`) rather than
recognised as the bug it is. Those allocation tests derive their limits
from the position actually opened, so they self-calibrate and stayed green
either way — which is exactly why they never caught this. The two new
tests assert the sized *value*: one pins risk-at-stop to
`limits.risk_per_trade_pct` (5.0% vs 0.5% pre-fix, a clean 10x), the other
sets `max_exposure_pct` between the real and the over-estimated notional
and asserts the trade is approved (pre-fix: "Projected exposure 15.61% vs
limit 3.5%"). Both verified to fail against the pre-fix engine.

## Realized P&L double-counted on every re-entry (§86)

`GET /portfolio`'s `total_realized_pnl` summed `Position.realized_pnl`
across every row for the account. That column is not an increment, and
summing it counts earlier round trips again.

`PositionManager.apply_fill` (`app/trading/position_manager.py`) keeps one
`PositionRecord` per `(account_id, symbol)` in `_positions` and does
`position.realized_pnl += realized` on it. Nothing ever removes or resets
that record when the position goes flat — `PositionManager.close()` is the
only thing that would, and no application code calls it. So the value is a
**lifetime running total** for the instrument, not the P&L of one position.

`persist_position` (`app/trading/persistence.py`) looks up only the row with
`is_open=True`. Once a position closes, that row is flipped to
`is_open=False`; the next entry in the same instrument therefore finds no
open row and **inserts a new one**, seeded with `realized_pnl=position.
realized_pnl` — the cumulative total the *previous* position earned.

Two closed round trips of +500 and +300 leave rows carrying 500 and 800.
The true total is 800; the sum is 1300. After N round trips on one
instrument the endpoint reports `N(N+1)/2 x p` instead of `N x p`, and it
inflates losses identically. Reproduced end to end through the real writers
(`PositionManager` -> `persist_position` -> `compute_portfolio_exposure`):

```
positions rows: [(500.0, False), (800.0, False)]
GET /portfolio total_realized_pnl : 1300.0
truth                             : 800.0
```

Separate instruments are unaffected — each keeps its own row chain — so
this is specifically re-entry on one instrument, which is the normal case
for a strategy that trades a symbol repeatedly, and the constant case for
`AutoTradeSupervisor`.

The fix reads the number from `trades` instead. Every realizing fill
already writes exactly one `Trade` row carrying the **delta**:
`pnl=realized_delta` in `app/api/orders.py` and `app/api/options.py`,
`pnl=outcome.closed_position_pnl` in `app/api/paper.py` and
`app/workers/auto_trade_worker.py`, each with an `execution_mode` matching
the position's. Those four are the only callers of `persist_position`, so
the journal covers every path that can realize anything, and it is correct
for partial closes and flips (which is what the earlier `min(filled,
abs(position_before))` work made true of the `quantity` on those same
rows). It also survives an API restart, which resets the in-memory counter
to zero and starts a fresh row chain — under the old aggregation the rows
written before and after a restart would both be summed.

The writer side is deliberately untouched. `Position.realized_pnl` still
holds the lifetime total, which is what the in-memory record means and what
the existing single-round-trip test compares against; the bug was reading it
as though it were per-row.

This is a defect in the earlier fix that made `total_realized_pnl` sum over
closed rows at all. That change was right that realized P&L cannot be read
from open positions only, and wrong about what the rows contain.

`tests/api/test_portfolio.py`'s existing
`test_portfolio_reports_realized_pnl_after_a_position_closes` cannot catch
it: it does exactly one round trip, so there is one row, and it reads that
row with `.scalar_one()` — which structurally asserts a single row ever
exists — then asserts the endpoint equals that same row's value, so
`SUM(realized_pnl) == row.realized_pnl` held no matter what the aggregation
did. The same `.scalar_one()` on `Position` appears throughout
`tests/api/test_orders.py`, so no test in the suite ever produced the
two-row state. `tests/risk/test_portfolio.py` does insert several rows
including a closed one, but leaves every `realized_pnl` at 0 and never
asserts `total_realized_pnl`.

The new `test_portfolio_realized_pnl_is_not_double_counted_across_re_entries`
runs two full round trips on one instrument through `POST /orders`, computes
the expected total from the quantity actually opened and the prices it
chose (never from the aggregation under test), asserts the row sum is
strictly greater than that expectation — pinning the overlap that makes the
rows unsummable — and asserts the endpoint reports the true figure. Pre-fix
it reports 2600 against a true 1600. The second round trip uses different
prices deliberately: `POST /orders` keys idempotency on
`{user}:{symbol}:{direction}:{entry}:{stop}`, so re-entering at the same
entry and stop is deduped into the first order and no second position is
opened at all.

## The options exposure gate was defeated by a net debit (§38-40)

`evaluate_options_risk` sizes a whole multi-leg combination by one number,
`risk_amount`, and feeds it to `exposure_limit`. When the payoff curve
bounds the downside it uses `abs(max_loss)`, which is right. When
`max_loss is None` -- the curve is still falling at a sampled edge, so the
loss is unbounded -- it used `PayoffResult.capital_requirement`, on the
stated assumption that this was "already its best estimate of worst-case
loss for an unbounded-risk combination".

It is not. `compute_payoff_summary` computes it as
`max(premium, 0.0) or abs(min(payoffs))`: for a net **debit** that is the
debit paid, and only for a net **credit** does it fall through to the worst
sampled loss. So the exposure check on an unbounded-risk position entered
for a debit was sized by its entry cost.

A long 25000 put plus a short 26000 call at lot size 50 is a synthetic
short: unlimited loss above 26000. Entered against a 100,000 balance with
the default `max_exposure_pct` of 100%:

| one leg's premium | net entry | `max_loss` | sized as | payoff at 39000 | decision |
|---|---|---|---|---|---|
| call at 30 | 1,000 debit | `None` | **1,000** | -651,000 | **APPROVE** at 1.00% |
| call at 60 | 500 credit | `None` | 649,500 | -649,500 | REJECT at 649.50% |

Raising one leg's premium by 10 rupees does not change the risk shape at
all, and flipped the account from "649% of equity at risk, blocked" to "1%
at risk, approved". A 1x2 front ratio call spread behaves the same way.
`POST /options/execute` builds its legs directly from the client payload
rather than through `build_strategy` -- deliberately, so arbitrary
combinations are supported -- so any account with `LIVE_TRADE` can reach
this shape, and both approved legs then go through the normal broker and
persistence pipeline with no further size gate.

The fix adds `PayoffResult.worst_sampled_loss`, set from the
`max_loss_sample` the function already computes, and reads it in
`evaluate_options_risk` as `max(-worst_sampled_loss, capital_requirement)`
when `max_loss is None`. The floor at `capital_requirement` keeps the
degenerate case -- a curve above zero everywhere in the sample but sloping
down at an edge -- from reporting zero risk for an unbounded position.

`capital_requirement` is deliberately left alone. It is returned by
`POST /options/payoff` and `POST /options/execute` as the capital needed to
enter, and for a debit strategy that really is the debit; overwriting it
with a worst-case-loss figure would fix the gate by making a user-facing
number wrong. A separate field also removes the overloading that caused
this in the first place -- the reader now names exactly what it needs.

The suite had a test called
`test_unbounded_risk_strategy_uses_capital_requirement_not_zero`, which
sounds like coverage of exactly this branch and is not: its fixture is a
long call, whose *profit* is unbounded while its loss is bounded at the
premium paid, so `max_loss is not None` and the branch never runs. Its own
docstring says so. `capital_requirement` appears nowhere in the test suite
except in that test's name, and no options fixture in the repo
(`long_call`, `bull_call_spread`, `bear_call_spread`, `iron_condor`) has an
unbounded loss, so the `max_loss is None` path had no coverage at all.

Three tests now cover it: the debit synthetic short must be rejected and
its projected exposure must read above 500%, not 1%; the debit and credit
variants of the same position must produce exposures within 2 percentage
points of each other (they differ by the premium, 1.5 points, rather than
by the ~648 points the old sizing produced); and a bounded spread must
still be sized by its real `max_loss`, pinned at exactly 3.5%. The first
two fail against the pre-fix code with APPROVE where REJECT is required.

## Paper/auto retest entries filled at the candle close, not at the level (§49, §54)

`PaperTradingEngine.on_candle` -- behind `POST /paper/{id}/candle` and every
autonomous trade -- submitted a MARKET order the instant a signal matched.
`set_quote(ltp=candle.close)` at the top of the method means the MockBroker
fills a MARKET order at that close. But a retest entry does not mean "buy
now": `fvg_retest` sets `entry` to the midpoint of an *unmitigated* fair
value gap, which by definition is a level price has not traded back into,
and `order_block_retest` does the same for an order block. Stop, target and
R are all derived from that `entry`.

So the trade was sized on `|signal.entry - signal.stop|`, bracketed around
`signal.entry`, and then opened at an entirely different price. On the
`SETUP` fixture in `tests/paper/test_engine.py`:

```
candle 7 = (o107, h110, l106, c109)
signal:   entry=103.0  stop=99.7  target=109.6
filled:   average_price=109.0  quantity=151.5152  stop=99.7  target=109.6
```

103 is not a price candle 7 ever traded at -- its low is 106. Three
consequences:

- **The risk cap is silently blown.** Sized on a 3.30 stop distance, the
  real distance from the fill to the stop is 9.30, so the trade risks
  **1.41%** of the account against a configured `risk_per_trade_pct` of
  0.5%. 2.8x, on the path that runs unattended.
- **Exposure checks understate the position**, since the notional the risk
  engine evaluated used the signal's entry rather than the fill price.
- **The bracket can be breached at entry.** Nudge that candle to
  `(107, 112, 106, 111)` -- same FVG, same signal -- and the position opens
  at 111 against a target of 109.6. The next candle takes the
  `take_profit` branch and closes at 109.6 for a **loss**, journaled as a
  `Trade` and pushed to the user as a TP_HIT notification reading
  "Realized P&L: -212.12".

And backtest and paper disagreed on the same strategy over the same
candles: the backtester opens at candle 8 at 103.0 and books +1000.00 at
R=2.0, while paper opened at candle 7 at 109.0 and booked +90.91 -- 11x
smaller for an identical setup, against a blueprint that requires the two
to share one evaluation and agree.

`app/backtest/engine.py` already has exactly the right gate --
`if result.matched and candle.low <= result.entry <= candle.high` -- added
when the same phantom-fill problem was fixed there. It was never carried
across to the paper engine, which is the sibling-state pattern again: a fix
applied to one of two engines that run the same strategies.

The fix mirrors it. `on_candle` returns early when the candle's own range
does not contain `result.entry`, and pins the quote to `result.entry`
before submitting -- the same thing `_maybe_exit` already does with the
stop/target level it closes at. Both halves are no-ops for
`entry.type == "market"`, where `result.entry` is `candle.close` and so
always inside `[low, high]`. The early return happens before the risk
evaluation, leaving `risk_checks` as None, so neither caller writes a
spurious `RiskEvent` row for a trade that never happened.

Three existing tests broke on the change and were corrected rather than
worked around: each drove candles until `signal.matched` and then stopped,
which is now the candle *before* the fill. They break on the risk decision
instead.

Worth recording plainly: the two tests added when paper sizing was last
fixed measured risk as
`quantity * abs(signal.entry - signal.stop) / balance`, and `quantity` was
itself `risk_amount / |signal.entry - signal.stop|`. Both sides came from
the same two numbers, so the assertion re-derived the percent that went in
and held for **any** fill price. It did catch which percent fed the sizing
-- that was the bug it was written for -- but it could never see that the
fill happened somewhere else. Both now measure from
`position.average_price`, the price the broker really filled at, and both
fail against the pre-fix engine at 1.4091% and 0.2818% against limits of
0.5% and 0.1%.

Two new tests: one asserts the filling candle actually traded at the level,
that the fill price equals the level and *not* that candle's close (they
differ, 103.0 vs 104, so it is not a tautology), and that a long never
opens at or above its own target; the other runs `BacktestEngine` and
`PaperTradingEngine` over the same candles with the same sizing percent and
asserts the realized P&L match exactly, both cost models being
frictionless. Pre-fix they fail at `106 <= 103.0` and 181.82 vs 2000.00.

## Replay "Average R" was measured against the moved stop (§43-44)

Blueprint §43 makes MOVE SL a first-class replay action, and §44 asks for
"Average R" in the statistics panel. The two did not coexist correctly.

`ReplayEngine.close` computed `r_multiple` as the profit per unit divided
by `abs(entry_price - trade.stop)`. `set_stop` (aliased as `move_stop`, and
reachable as the `set_stop` action on `POST /replay/{id}/action`)
overwrites `trade.stop` every time it is called, so the denominator was
wherever the stop had been moved to by the time the trade closed -- not the
risk the trade actually took.

R is reward expressed in units of the risk accepted at entry. That number
is fixed the moment the stop is first placed; a stop moved afterwards
changes the trade's *outcome*, never its unit of measure. Measuring against
the current stop makes R mean something different for every trade and
destroys the one property that makes it worth reporting -- comparability.

Entry at 100 with a stop at 90 is 10 per unit of risk. Both harms
reproduced against the engine:

```
initial risk 10/unit                 pre-fix              true
stop left at 90    exit 130, +30     r=3.0  avg_r=3.0     3.0   (unaffected)
stop trailed to 120  exit 120, +20   r=1.0  avg_r=1.0     2.0
stop at breakeven  exit 130, +30     r=None avg_r=None    3.0
```

The breakeven row is the sharper one. Moving the stop to entry is the most
common trade-management action there is, and it leaves `stop ==
entry_price`, which the `entry_price != stop` guard treats as "no risk to
measure against" -- so `r_multiple` stayed `None` and the trade was dropped
from `average_r` entirely. The statistic silently excluded exactly the
trades the user had managed well, and reported `None` for a session made
entirely of them.

The fix adds `ReplayTrade.initial_stop`, set by `set_stop` only when it is
still `None` and never overwritten, and computes R from that. The persisted
`replay_orders.stop` column keeps holding the current stop, which is what
it means; `r_multiple` is not persisted, so no migration is involved.

`tests/replay/test_engine.py` never called `set_stop` twice -- no test in
the suite moved a stop at all -- and no test asserted `r_multiple` or
`average_r` at any point. `test_statistics_after_a_losing_and_a_winning_trade`
checks trades, win rate, best and worst, and its winning trade has no stop
set at all, so R was `None` there regardless. Three tests now cover it: a
trailed stop that exits at 2R, a breakeven-managed winner that must still
count as 3R, and an unmanaged trade whose R must be unchanged.

## A winning replay session wedged itself on `Infinity` (§44)

`compute_statistics` returned `float("inf")` for `profit_factor` when a
session had winners and no losers, and went out of its way to preserve it
(`... if isinstance(profit_factor, float) and profit_factor != float("inf")
else profit_factor`). Its sibling `app/backtest/metrics.py` has always
returned `None` in the same situation. Replay is the one that diverged, and
unlike the backtester its statistics get **persisted**.

`sync_replay_session` writes the whole statistics dataclass into
`ReplaySession.stats`, which is a Postgres `json` column. SQLAlchemy's JSON
bind processor is plain `json.dumps` with `allow_nan=True`, so an infinity
is emitted as the bare token `Infinity` -- which Postgres rejects:

```
json.dumps({"profit_factor": float("inf")})  ->  {"profit_factor": Infinity}
SELECT CAST('{"a": Infinity}' AS json)
  -> asyncpg.exceptions.InvalidTextRepresentationError:
     invalid input syntax for type json.  DETAIL: Token "Infinity" is invalid.
```

So the commit raised, and `get_db` has no handler for it. Driven through
the real API against the existing replay fixture -- buy 10 at candle 0's
close of 100, set a target of 102, step onto candle 1 whose high is 102:

```
create: 200   buy: 200   set_target: 200   step: 500
```

One winning trade with no losing trade wedges the session. Every
subsequent `/step` and `/order` returns 500, for exactly as long as every
closed trade is a winner -- that is, for a user who is doing well. It
cannot clear itself either: the only thing that would make `gross_loss > 0`
is recording a losing trade, and `/order` 500s too. Only `POST /reset`,
which throws the session's whole history away, gets out.

There is durable damage as well. `_upsert_replay_order` sets
`exit_price`/`pnl`/`closed_at` in the same aborted transaction, so the
`replay_orders` row for the winning trade stays at its last committed state
-- `closed_at IS NULL`, `pnl IS NULL` -- permanently contradicting the
in-memory engine, which is precisely what `sync_replay_session`'s docstring
("the persisted row never drifts from the live in-memory state") promises
cannot happen.

The fix makes replay agree with the backtester: `None` when there are no
losses to divide by. Nothing is lost by dropping the sentinel -- pydantic's
default `ser_json_inf_nan` already serialized it as `null` on
`GET /replay/{id}`, so no client ever saw an infinity; it existed only long
enough to break the write path.

Neither existing persistence test could reach the state. The first buys at
candle 0's close of 100 and closes at candle 3's close of 98 -- a loss, so
`gross_loss > 0` and `profit_factor` is a finite 0.0 -- and its only P&L
assertion is `balance != starting_balance`, which asserts that the balance
moved but never which way; making that fixture profitable would have kept
the assertion true while 500ing. The second opens and closes on the same
candle for a P&L of exactly 0, taking the `gross_profit == 0` branch
instead. The in-memory engine tests do build all-winner sessions and even
call `.statistics`, but with no database and no HTTP serialization an
infinity is an ordinary float there and nothing notices.

The new `test_a_winning_only_session_still_steps_and_persists` drives a
winner-only session through the API, asserts the step returns 200, and
asserts the persisted row caught up: `stats["trades"] == 1`,
`profit_factor is None`, balance above starting balance, and the
`replay_orders` row carrying a non-null `closed_at` and a positive `pnl`.
Pre-fix it fails with `Token "Infinity" is invalid`.

## Weekly candles opened on Thursday (§14, §16)

`bucket_start` derived every timeframe boundary from the Unix epoch:
`minutes_since_epoch // target_minutes`. That is right for every bucket
that divides a day evenly -- the epoch boundary and the midnight-UTC
boundary coincide -- and wrong for the one that does not. 1970-01-01 was a
**Thursday**, so a `1W` bucket ran Thursday to Wednesday:

```
2026-01-05 (Mon) -> weekly bucket 2026-01-01 (Thu)
2026-01-07 (Wed) -> weekly bucket 2026-01-01 (Thu)
2026-01-08 (Thu) -> weekly bucket 2026-01-08 (Thu)
```

A weekly candle built on that grid opens with Thursday's open, closes with
the following Wednesday's close, and carries the weekend in its middle
rather than at its edge. `1W` is in `SUPPORTED_TIMEFRAMES` (blueprint §16),
and a weekly open/close is exactly the sort of level SMC and ICT
methodology reads bias from, so the bar is not merely shifted -- it is
about a different week than the one anyone reading it means.

The codebase already disagreed with itself about this.
`app.smc.liquidity.detect_session_levels` buckets previous-week highs and
lows by `isocalendar()`, and `RiskWindow.roll` resets `weekly_pnl` on an
ISO week boundary. Both start Monday. Only the candle grid started
Thursday, so a weekly candle and the weekly liquidity levels drawn on it
could belong to different weeks.

The fix anchors weekly buckets on Monday and leaves sub-weekly buckets on
their (correct) epoch arithmetic. `CandleWorker._completes_bucket` was
re-deriving the same grid independently with its own epoch modulo, so it
is now expressed in terms of `bucket_start` -- a base candle completes its
bucket exactly when the next one falls into a different bucket. That holds
for any anchoring and removes the possibility of the two drifting apart
again.

**Honest scope:** this is a latent defect, not a live incident.
`resample_candles` has no production caller today (it is exported from
`app.market` and used only by tests), and `CandleWorker.derived_timeframes`
defaults to `[]`, so nothing currently derives a weekly candle. It becomes
a wrong number the moment anyone configures the worker with `1W` or calls
the exported helper -- both ordinary uses of shipped, blueprint-listed
API. It is recorded here as a correctness fix with an unambiguous right
answer, not as something that was silently corrupting data in production.

`tests/market/test_aggregation.py` covered only 1m -> 5m resampling and a
downsampling rejection; no test exercised daily or weekly bucketing at
all, which is why an entire timeframe could be wrong without a failure.
Three tests now cover it: every instant Monday through Sunday maps to that
Monday (and Thursday no longer opens a bucket); two timestamps share a
weekly bucket exactly when they share an ISO week, checked across a
30-day span that crosses a year boundary; and a Monday-to-Friday run of
daily candles resamples into exactly one weekly bar opening on the Monday
with Monday's open and Friday's close. Pre-fix the first maps Monday to
2026-01-01, the second disagrees on the Sunday/Monday pair, and the third
produces two bars instead of one.

## The risk gate locked users into losing positions (§56-57)

`RiskEngine.evaluate` runs on every order `POST /orders` receives, and
every one of its exposure, loss and count checks is an **entry** check --
`max_open_positions` is literally phrased `f"{proposal.open_positions} open
vs limit {limits.max_open_positions}"`. `TradeRiskProposal` carried nothing
saying whether an order opens exposure or reduces it, and the gate ran
about fifty lines before the request path looked up `existing_position`,
so it could not have known.

The result inverts the purpose of the limits. Reproduced end to end
through the API on default limits (0.5% per trade, 2% daily loss):

```
open A (long 100)                  -> 201
open B (long 100)                  -> 201
close B at a 5,000 loss            -> 201
CLOSE A (the exit that matters)    -> 403 Daily loss 5.00% vs limit 2.0%
still open: [(100.0, 'A')]
```

The user is now holding a losing position and is refused the one order
that would end the loss. This is not recoverable by another route:
`POST /orders` is the only way out, since `app/api/positions.py` and
`app/api/portfolio.py` are read-only and `POST /orders/{id}/cancel`
rejects anything not still `SUBMITTED`/`ACKNOWLEDGED`. There is no live
stop enforcement either (recorded elsewhere in this document), so nothing
else will close it. `_UserTradingStack.daily_pnl` clears only at a **UTC**
day boundary, which for an NSE session (03:45-10:00 UTC) falls after the
close -- so the position is frozen for the rest of the trading day while
its loss runs on.

`max_open_positions` and `max_trades_per_day` trap the same way: at five
open positions no sixth order can be placed, including the one that closes
the fifth; after ten orders in a day, nothing can be exited. And the halt
check returns 423 `"New entries are halted for this account"` for exits
too -- reconciliation halts an account precisely when its positions look
wrong, which is the worst moment to also forbid closing them.

The fix establishes, before both the halt check and the gate, whether the
order opposes an open position, and treats such an order as reducing:

- Its sized quantity is clamped to `abs(existing.quantity)`. This is what
  makes the exemption safe rather than a hole -- an exempted order can
  then only reduce or flatten, never open or flip. Without the clamp,
  `is_reducing` would be a way to launder a fresh entry past every limit
  by sending it as a larger opposing order.
- `TradeRiskProposal.is_reducing` skips exactly the seven entry-only
  checks: `daily_loss_limit`, `weekly_loss_limit`, `exposure_limit`,
  `strategy_allocation_limit`, `correlated_exposure_limit`,
  `max_open_positions`, `max_trades_per_day`. They are skipped rather than
  recorded as passed, so the `RiskEvent` audit row lists exactly the
  checks that actually governed the decision.
- Everything about whether *this* order can execute sanely right now still
  applies: `kill_switch` (a deliberate human stop), `valid_stop_distance`,
  `entry_matches_market`, `liquidity_acceptable`, `market_data_fresh`,
  `broker_healthy`, `no_repeated_rejections`, `no_abnormal_price_jump`.
- The halt check is gated on `not is_reducing`, matching its own wording.

One behaviour change falls out of the clamp: a single order can no longer
flip a position from long to short. It flattens instead, and the new entry
is a separate, fully-checked order. That is the correct shape -- the
opening half of a flip is an entry and should face the entry limits.

**Not fixed here:** `POST /options/execute` has the same defect, running
`evaluate_options_risk` unconditionally on closing legs. It is left for
its own change rather than half-done: a multi-leg order needs its own
definition of "reduces exposure" (what if two legs close and one opens?),
and guessing at that inside this change would be worse than naming it.

Why the tests missed it: `test_daily_loss_limit_is_enforced_after_a_real_realized_loss`
looks exactly like coverage of this state and cannot reach it -- it opens
and closes the *same* instrument, so the account is flat when the limit
trips, and its third order is a fresh entry.
`test_place_and_close_order_persists_to_database` does close a position,
but with `daily_pnl` 0 and one position open, so every entry gate passes
anyway. And `tests/risk/test_engine.py`'s limit tests could not express
"this is an exit" at all, so they structurally could not tell correct
behaviour from broken. No test anywhere placed a closing order while a
limit was tripped.

Five tests now do. Two at the API: a second position must still be
closable once the daily loss limit trips (403 pre-fix), and an oversized
opposing order must leave the account flat rather than short, proving the
clamp holds. Three at the engine: a reducing proposal against a fixture
that fails *every* entry limit must be approved and must run exactly the
execution-sanity set -- asserted as set equality, so a future check added
on the wrong side of the fence fails the test -- and must still be stopped
by the kill switch and by an unhealthy broker.

## The same exit trap on POST /options/execute (§37-40, §56-57)

The companion to the `POST /orders` fix recorded above, and the half that
was deliberately left out of it. `evaluate_options_risk` ran
unconditionally on every order, and its `exposure_limit`,
`daily_loss_limit`, `weekly_loss_limit`, `max_open_positions` and
`max_trades_per_day` checks are all entry checks -- `max_open_positions`
is phrased "N open vs limit M" here too. The gate also ran well before the
per-leg `existing_position` lookup inside the execution loop, so it could
not have known the order was a close. `POST /options/execute` is the only
path that closes an options position, so the holder of a losing spread was
refused the one order that would end the loss. Confirmed by running the
new tests against the pre-fix code: `assert 403 == 201`, twice.

The reason this was not folded into the previous change is that a
multi-leg order needs a definition of "reduces exposure" that a
single-instrument order does not, and guessing at one inside that change
would have been worse than naming the gap. The definition settled on here:

**An options order is reducing only if _every_ leg opposes an open
position in that leg's own instrument.** All-or-nothing, deliberately. If
any leg opens exposure the whole order is an entry and faces the full
gate, so a fresh position can never ride in alongside a genuine close --
which is exactly the "two legs close and one opens" case that made the
question worth deferring. The conservative answer costs a user nothing:
they close the spread, then open the new position as its own
fully-checked order.

Each reducing leg is additionally clamped to `abs(existing.quantity)`
where it is submitted. That clamp is what makes the exemption safe rather
than a hole, the same way it does on the equities path: without it, a
client could clear every limit by sending an oversized opposing leg and
calling it a close. Verified: five lots sent against one open lot leave
the leg flat, not four lots short.

The exempt set mirrors the equities fix -- exposure, both loss limits, and
both count limits are skipped (skipped, not recorded as passed, so the
`RiskEvent` row lists the checks that governed the decision). Everything
about whether these legs can be executed sanely right now still applies:
the kill switch, repeated rejections, liquidity, premium deviation, market
data freshness and broker health. The halt check is gated on
`not is_reducing`, matching its own "New entries are halted" wording.

Neither existing options test could reach the state: every one of them
either opens a spread with a clean account, or asserts a rejection on a
fresh entry. None placed a closing order while a limit was tripped -- the
same fixture-cannot-reach-the-breaking-state gap that hid this on the
equities path.

## An unvalidated `direction` traded declared longs as shorts (§33-34)

`StrategyDefinition.direction` and `Condition.direction` were bare `str`
fields with the vocabulary only in a comment (`"bullish" | "bearish"`).
Every consumer reads them as a bare equality test with an implicit bearish
`else`:

```
app/strategy/engine.py   is_bullish = direction.lower() == "bullish"
app/paper/engine.py      Direction.LONG if result.direction.lower() == "bullish" else Direction.SHORT
app/backtest/engine.py   is_long = result.direction.lower() == "bullish"
```

So **every** value that is not literally `"bullish"` silently meant
*bearish* -- including `"long"`, a typo, and the empty string. Verified
against the engine on a 90-120 dealing range with price at 100:

```
'bullish'  matched=True dir='bullish' entry=100 stop=90.0  target=120.0
'LONG'     matched=True dir='LONG'    entry=100 stop=120.0 target=60.0
'long'     matched=True dir='long'    entry=100 stop=120.0 target=60.0
'bearish'  matched=True dir='bearish' entry=100 stop=120.0 target=60.0
```

A strategy the user named and declared long evaluates with the stop above
entry and the target below, and the paper and autonomous engines open a
short from it.

`"LONG"` is not a far-fetched typo. This platform's own order endpoints
take the **identically-named** `direction` key with the *other* vocabulary
and enum-validate it (`Direction` LONG/SHORT in `app/api/orders.py` and
`app/api/options.py`), so `POST /orders` refuses `"bullish"` while
`POST /strategies` accepted `"LONG"` and traded it short. Two vocabularies,
one key name, one of them unchecked.

Exactly one of the four consumers noticed the boundary existed:
`app/workers/scanner_worker.py` keeps a `_BIAS_TO_TRADE_DIRECTION` map and
logs "Unrecognized strategy direction" rather than writing a `Signal`. Its
three siblings coerce to short. The sibling-divergence pattern again, and
`app/strategy/dsl.py` already had the right precedent in
`_reject_unimplemented_boolean_operators` -- a `field_validator` on
`Condition.operator`, added for this same bug class. The neighbouring
field was left open, and this failure mode is the worse of the two: that
one made a strategy never fire; this one makes it fire inverted.

The fix adds a `field_validator` on `direction` for both models, rejecting
anything outside the bias vocabulary and normalizing case and whitespace,
so `POST /strategies`, `PUT /strategies/{id}` and
`app.ai.strategy_builder.parse_strategy_json` all fail with a 422 naming
both vocabularies. `None` still means "either -- take the SMC bias".

`Condition.side` and `Condition.zone` are the same shape of unvalidated
free text and are deliberately **not** included: an unrecognised value
there never satisfies its condition (`app/strategy/evaluator.py`), so they
fail closed into no trade rather than an inverted one. Worth tightening
some day; not the same defect.

**Live**, on the paper, auto-trade, backtest and `POST /scanner` paths --
any authenticated user can create such a strategy today with no error
anywhere. No real-money impact yet, because autonomous trading drives
`MockBroker` and `POST /orders` does not consume the strategy DSL; the
damage today is inverted simulated positions, inverted journaled `trades`
rows, and inverted backtest results used to decide whether to promote a
strategy to auto-trading. It becomes a real-money defect the moment a live
broker is wired into the autonomous path.

Coverage: `tests/strategy/test_dsl.py` existed solely for this bug class
and never touched `direction`. Every strategy fixture in the suite --
`tests/strategy/test_engine.py`, `tests/paper/test_engine.py`,
`tests/backtest/test_engine.py`, and the backtest/AI API tests -- pins
`"bullish"`, so the bearish branch had no coverage at all and
`test_market_entry_inside_the_dealing_range_still_emits_a_valid_long`
asserts exactly the invariant that breaks (`stop < entry < target`) from a
fixture that can never reach the failing state. Three parametrized DSL
tests now reject nine bad values across both models and pin the
normalization, and a new engine test pins the polarity of both biases as
mirror images -- the fork the bug routed into, which nothing had asserted.

## A LIMIT order with no limit price filled at 0.0 (§59-60)

`PlaceOrderRequest.price` is optional for every order type and had no
cross-field validation, and `MockBroker._resolve_fill_price` read it as
`request.price or 0.0` for anything that is not MARKET. So a LIMIT order
carrying no limit price was accepted and **filled at zero**. Driven
through the real API:

```
POST /orders (LIMIT, no price) -> 201 status=MONITORING qty=100.0
GET /positions -> [{quantity: 100.0, average_price: 0.0, unrealized_pnl: 0.0}]
GET /portfolio -> {open_position_count: 1, total_exposure: 0.0}
# then close it with an ordinary MARKET short at 100:
GET /portfolio -> {open_position_count: 0, total_realized_pnl: 10000.0}
```

A fill at zero is not a cheap fill. The position carries
`average_price=0.0`, so `compute_portfolio_exposure` computes
`abs(qty * average_price)` as **0** and reports a real 100-unit position
as zero exposure -- and `place_order` feeds that same 0 into
`current_exposure` for every later `exposure_limit` check. Closing the
position then books the entire notional as realized profit: 100 units
"bought at 0" and sold at 100 journals a 10,000 gain into `trades` and
into `daily_pnl`/`weekly_pnl`, which *loosens* the daily and weekly loss
budgets. That is the same harm the inverted-bracket guard in
`app/strategy/engine.py` was added to stop, arriving by a different door.

MARKET was never affected: it fills from the broker's own quote, seeded
from `entry`.

The fix refuses rather than fabricates, at both layers:

- `PlaceOrderRequest` gains a model validator. LIMIT requires a positive
  `price`. **SL and SL_M are refused outright**, because their trigger
  cannot be expressed end to end: the request model has no
  `trigger_price`, `OrderRecord` does not carry one, and
  `ExecutionEngine.submit` does not pass one -- so `app/brokers/upstox/
  adapter.py`, which does map both types and does send a `trigger_price`
  field, would send `0` to a real broker. `OrderRequest` and the adapter
  are ready for them; wiring the field through the three layers between
  is a feature, and until it exists, refusing is the honest answer.
- `MockBroker._resolve_fill_price` returns `None` instead of `0.0` when
  there is no usable price, and `place_order` turns that into an ordinary
  broker rejection -- a path the execution engine already understands. A
  caller that cannot say what price it wants now gets a rejection it can
  see rather than a position it cannot explain. This also closes the
  MARKET-with-no-quote-anywhere case, which resolved to 0.0 too.

Coverage was the "every fixture pins the same enum value" shape in its
purest form. The only fill-price assertion anywhere in the suite is in
`tests/trading/test_execution.py`, whose fixture hardcodes
`order_type=OrderType.MARKET`; no test in `tests/api/` ever sent an
`order_type` or `price` field at all, so every one of the suite's ~40
order placements took the MARKET branch; and there was no
`tests/brokers/test_mock.py` -- `_resolve_fill_price` had no direct test.
The whole non-MARKET branch was unexecuted.

There is now a `tests/brokers/test_mock.py` pinning the broker contract
directly (unpriced LIMIT/SL/SL_M rejected leaving no position; a
non-positive price refused; a LIMIT filling at its own price and not at a
deliberately different quote; MARKET still filling from the quote; and
MARKET with no quote and no price rejected), plus two API tests: the
three unpriced order types must 422 and leave no `orders` or `positions`
rows behind, and a LIMIT *with* a price must fill at that price.

## Entry types outside the DSL's vocabulary (§33-34)

`EntryConfig.type` was a bare `str` defaulting to `"market"`, and
`app/strategy/engine.py`'s `_resolve_entry_and_stop` read it as a chain of
bare equality tests with an implicit `else`: `== "fvg_retest"`, then
`== "order_block_retest"`, then fall through to a market entry at
`context.current_price` with the stop at the dealing-range edge. Every
value that was not *exactly* one of those two strings took the fallback
silently.

That is not a near miss, it is a different trade. On one set of candles
carrying an unfilled bullish FVG at 99-103, a confirmed swing low at 96
and a swing high at 111, with price at 105:

```
entry.type='fvg_retest'   entry=102.75  stop=99.17  target=109.90   stop 3.48% away
entry.type='FVG_RETEST'   entry=105.00  stop=96.00  target=123.00   stop 8.57% away
```

One character of case. The substitute chases price at the top of the move
rather than waiting for it to come back to the gap, brackets itself
against a level two and a half times further away, and -- because the
retest gates added to `app/paper/engine.py` and `app/backtest/engine.py`
only fill when `candle.low <= entry <= candle.high` -- opens a position on
a candle where the strategy as written would have taken none at all. Note
that position *sizing* still solves for the configured risk percent, so
the currency at risk is unchanged; what changes is which trade is taken,
at what price, against what invalidation level. A backtest run to validate
the strategy before enabling it exercises the substitute too, so it never
reveals the swap.

`POST /strategies` returned 201 for `{"type": "FVG_RETEST"}` and persisted
it verbatim. So did `PUT /strategies/{id}`, and so did
`app.ai.strategy_builder.parse_strategy_json` -- which matters more than a
human typo, because blueprint §32 makes the AI a first-class producer of
these documents and its system prompt tells it to "Only use condition
types, operators, and entry types the schema defines". The schema defined
the first two and not the third.

This is the third field in this DSL to fail this way and the last of the
three that prompt names. `Condition.operator` (AND/OR/NOT, declared but
unimplemented) and `direction` (the bullish/bearish bias vocabulary) are
already rejected at the model boundary. `Condition.side` and
`Condition.zone` remain deliberately unvalidated because they fail
*closed* -- an unrecognised value simply never satisfies its condition,
producing no trade. `entry.type` failed *open*, into a live position,
which is what separates it from them.

The fix is a `field_validator` on `EntryConfig.type` accepting exactly
`market`, `fvg_retest` and `order_block_retest`. Case and surrounding
whitespace are normalised rather than rejected, matching `_validate_bias`
in the same module, so `"FVG_RETEST"` now resolves to the retest entry it
names instead of to a market entry. Everything else raises, which means
422 from both strategy endpoints and a `StrategyBuilderError` from the AI
path.

Two consequences worth stating plainly. First, a strategy row already
persisted with an unrecognised entry type now fails
`StrategyDefinition.model_validate` on load; `ScannerWorker` and
`AutoTradeSupervisor` both already catch that, log it and skip the
strategy, so such a row stops trading rather than continuing to trade as
something it never said it was -- and `GET /strategies` still returns it
(the response model carries `definition` as a plain `dict`) so it can be
corrected with a `PUT`. Second, `EntryConfig.params` is accepted,
persisted and read by nothing: no entry type takes parameters yet, since
every entry and stop is derived from zone geometry alone. That is a
feature gap rather than a defect -- nothing is *mis*-interpreted by it --
and it is left in place because the DSL is a stored, versioned document.

Coverage was the "every fixture pins a valid value" shape: of the
seventeen strategy fixtures across the suite, sixteen spell a retest type
exactly and the seventeenth (`tests/strategy/test_engine.py`'s inverted-
bracket regression) spells `"market"`. Every one of them names a type the
engine recognises, so the fallback branch was only ever reached by the
fixture that meant to reach it -- never by one carrying an unrecognised
value, which is the case that mattered. The
new tests cover the validator directly (rejected values, normalised
values, and the unchanged `"market"` default), the behavioural difference
in `tests/strategy/test_engine.py` (a case-typo'd retest now resolves to
the same entry/stop/target as the canonical spelling, while the market
fallback demonstrably does not, has a bracket more than twice as wide, and
fills on a candle the retest does not), and the API boundary in
`tests/api/test_strategy_versions.py` (three bad types must 422 and leave
no rows behind; `"FVG_RETEST"` must 201 and come back normalised; `PUT`
must reject too).

## Sizing a LIMIT order on the price it fills at (§56-57)

`PlaceOrderRequest` carries two prices with different meanings. `entry` is
a claim about the current market, and `price` is the limit price. Sizing
and every notional risk check read `entry` for all order types -- but only
a MARKET order fills there. A LIMIT order fills at `price`, which was
passed straight through to the broker and compared to nothing: not to
`entry`, not to the broker's quote, not to `max_entry_deviation_pct`.

The result, driven through the real API on a fresh 100,000 account at the
default 0.5% risk per trade:

```
POST /orders {"direction":"LONG","order_type":"LIMIT",
              "entry":100.0,"stop":95.0,"price":5000.0}
  -> 201 status=MONITORING quantity=100.0
GET /positions  -> [{quantity: 100.0, average_price: 5000.0}]
GET /portfolio  -> {balance: 100000.0, total_exposure: 500000.0}
```

The risk engine sized `500 / |100 - 95| = 100` units and measured the
position as `100 x 100 = 10,000` notional -- 10% of the account, against a
100% exposure cap. The account then held **500,000, or 500% of its own
balance**, with `100 x |5000 - 95| = 490,500` at risk on an order approved
as 0.5% risk. The persisted `RiskEvent` row for it reads `APPROVE` with
`exposure_limit`, `entry_matches_market` and `valid_stop_distance` all
`True`.

`entry_matches_market` could not help. For `MockBroker` the quote is
seeded from `payload.entry` a few lines earlier, so that check is 0.00% by
construction -- the code's own comment says so. It guards `entry` against
the market; nothing guarded `price` against anything.

It does not take an adversarial payload. `entry=100, stop=105, price=90`
on a short is an ordinary resting limit: sized `500 / |100 - 105| = 100`
units, carrying `100 x |90 - 105| = 1,500` of real risk -- three times the
configured cap, silently. The wrong `average_price` then flows into
`positions`, `GET /portfolio`'s `total_exposure`, every later
`current_exposure` the risk gate reads, and on close into `Trade.pnl` and
`stack.daily_pnl`, which is what the daily and weekly loss halts are
measured against.

The fix is one value. `fill_price` is `payload.price` for a LIMIT order
and `payload.entry` otherwise, and it feeds both `calculate_position_size`
and `TradeRiskProposal.entry`. A LIMIT order fills at its limit price or
better -- a buy pays at most that, a sell receives at least it -- so it is
simultaneously the value that determines the fill and the conservative
worst case in either direction. Post-fix the same call sizes 0.102 units,
`total_exposure` is 510, and the risk actually taken on is 500.00, exactly
the configured 0.5%.

Two decisions worth recording, because the obvious wider fixes are wrong:

- The deviation check is deliberately **not** retargeted at `price`.
  `max_entry_deviation_pct` exists to catch a forged claim about the
  current market, which is what `entry` is. A limit price is a *chosen*
  price, not a claim; forcing it within 1% of the quote would refuse every
  legitimate resting limit order. Once sizing reads the fill price, no
  bypass remains without it.
- The fix is not in `MockBroker`. Filling a LIMIT at its own price is
  correct broker behaviour and `tests/brokers/test_mock.py` deliberately
  pins it. The disagreement was always in the endpoint, between the price
  it sized on and the price it sent.

The idempotency key grows a `price` component for the same reason: it was
`{user}:{symbol}:{direction}:{entry}:{stop}`, and now that the limit price
decides both the fill and the size, two orders differing only in it are
two different orders. Without it the second silently deduped onto the
first and returned its fill.

Scope, honestly. The unbounded version above is specific to `MockBroker`,
which fills a LIMIT instantly at any price regardless of its own quote --
a price the simulated market never traded at. That is the default broker
for every account here and it writes real `orders`/`positions`/`trades`
state. Against a real adapter (`app/brokers/upstox/adapter.py` sends
`price` as the limit) a resting limit fills at or better than `price`, so
the pre-fix error flips sign into consistent under-sizing rather than a
bypass -- still a wrong number, and still fixed by the same change. This
was reasoned from the adapter's payload mapping, not verified against live
Upstox. The sibling entry paths were never affected: `PaperTradingEngine`,
`AutoTradeSupervisor` and `POST /options/execute` only ever submit
`OrderType.MARKET`, where the fill price is the seeded quote is `entry`.
`POST /orders` is the only path where the sized price and the filled price
can differ, and it was the only one with nothing tying them together.

One thing this does *not* fix: a LIMIT price on the wrong side of the stop
(`LONG entry=100 stop=95 price=90`) still produces an inverted bracket.
That is the already-recorded gap that `TradeRiskProposal` carries no
`direction`, so `valid_stop_distance` measures `abs(entry - stop)` and
cannot tell which side anything is on. The inversion was equally present
before this change -- the position really was long at 90 under a stop at
95 -- and this change only makes the recorded numbers reflect it.

Coverage was the "assertion that is direction-free" shape.
`tests/api/test_orders.py::test_a_limit_order_with_a_price_fills_at_that_price`
was the only LIMIT test on the endpoint, and its fixture pinned
`entry=100.0, price=98.5` -- a gap too small to show anything -- asserting
`average_price == 98.5` and `quantity > 0`. It checked *that* the fill
price differs from `entry` while never checking that the quantity was
solved from the price actually filled, and `quantity > 0` holds just as
well for the wrong number. No test anywhere asserted that the approved
notional matches the resulting `average_price x quantity`. That test now
asserts the exact size, joined by three new ones (the 5,000 bypass with
its portfolio numbers, the ordinary 3x short, and the dedup case) and a
control that pins MARKET sizing as unchanged.

## One row per instrument symbol (§9-13)

`instruments` is a global, un-owned table -- no `user_id` -- and every
reader of it resolves a symbol with `.scalar_one_or_none()`:

- `app/api/orders.py::_get_instrument_by_symbol`, whose own docstring
  states the contract, called as the **first statement** of both
  `place_order` and `cancel_order`, and by `POST /paper`
- `app/api/options.py`'s per-leg lookup for `POST /options/execute`
- `app/risk/portfolio.py::compute_correlated_exposure`
- `app/trading/portfolio_snapshots.py`'s Greeks aggregation

"Exactly one row per symbol" was therefore already the operative contract
across the whole codebase. Nothing enforced it. `POST /instruments` did no
existence check, `Instrument.symbol` was `index=True` but not `unique=True`
with `__table_args__ = ()`, and the initial migration created the index
with `unique=False`. A second row for one symbol made every reader above
raise `MultipleResultsFound`, which the catch-all handler in `app/main.py`
turns into a 500.

Driven through the real API with two separate users:

```
first  POST /instruments        201
victim opens LONG               201  MONITORING qty=100.0
GET /positions                  [{quantity: 100.0, average_price: 100.0}]
second POST /instruments        201        <-- should be 409
GET /instruments?symbol=X       2 rows for one symbol
victim tries to CLOSE           500  Internal server error
GET /positions                  [{quantity: 100.0, ...}]   still open
```

The lockout is the sharp end of it. `POST /orders` is, by its own comment,
the only way to leave a position; `cancel_order` refuses anything already
filled, and `/positions` and `/portfolio` are read-only. The
reducing-order exemption exists precisely so that an exit is never refused
-- but it is unreachable here, because `_get_instrument_by_symbol` runs
*before* `is_reducing` is computed. The holder is left in an open
100-unit position with no exit path through the API at all, and since
there is no endpoint to delete or deactivate an instrument, recovery needs
direct SQL.

This needs no malicious actor. The endpoint is gated on plain
`get_current_user`, not the admin role `app/api/admin.py` uses, so any
user can register any symbol -- but the likelier trigger is an operator
bootstrapping the instrument master and re-running the same registration
call twice, which permanently bricks that symbol for the entire
deployment.

The fix is in three places, because a check in only one of them would be a
half-measure:

- `Instrument.symbol` becomes `unique=True`, with migration
  `b7c1d40e9a52` swapping the index for a unique one. This is what
  actually makes the readers' assumption true.
- `POST /instruments` looks the symbol up first and returns **409** with
  the existing row's id, rather than letting the constraint surface as a
  500.
- That lookup is read-then-write and so cannot be the guarantee on its
  own -- two concurrent registrations can both pass it -- so an
  `IntegrityError` on commit is caught and turned into the same 409.

Note this makes `symbol` globally unique rather than unique per exchange.
That matches what the readers actually do (none of them qualifies by
`exchange`), and listing one ticker on two exchanges would require
changing those five call sites, not just this constraint.

The migration deliberately does **not** dedupe existing rows. If a
database already contains duplicate symbols it will fail with a unique
violation, and that is the right outcome: duplicate instrument rows can
each own `candles`, `orders`, `positions` and `trades` via
`instrument_id`, so deciding which row survives and what becomes of the
other's children is an operator judgement, not something a schema
migration should make silently. The migration docstring carries the query
that lists them.

Coverage was absent rather than weak: `grep -rn '/instruments'` over the
test suite returned nothing at all -- both the create and list endpoints
had zero tests. And every test that needs an instrument inserts the row
directly with a uuid-suffixed symbol (`f"ORD{uuid.uuid4().hex[:6]}"` and
friends, across `test_orders.py`, `test_paper.py`,
`test_options_execute.py` and `workers/conftest.py`), so the suite was
uniquely-symbolled *by construction* and structurally could not reach the
duplicate case. There is now a `tests/api/test_instruments.py` covering
the 409 and the surviving single row, the lockout scenario end to end (a
holder with an open position can still close it after someone re-registers
the symbol), the database constraint itself independent of the API, and a
control that distinct symbols still register normally.

## Disconnecting a broker account that has traded (§70, §120)

`DELETE /brokers/{account_id}` hard-deleted the `broker_accounts` row.
That worked for as long as `orders.broker_account_id` was NULL for every
row -- which it was, until `resolve_broker` started returning the account
id so a placed order could be traced back to the account that executed it.
From that point every order stamped the column, the foreign key
(`ON DELETE NO ACTION`, verified against `pg_constraint` on the running
database rather than inferred) began refusing the delete, and the
resulting `IntegrityError` went uncaught into the catch-all handler:

```
POST /brokers/connect {"broker":"PAPER","credentials":{}}   201
DELETE /brokers/{id}          (no orders yet)               204
POST /brokers/connect                                       201
POST /orders                                                201
  orders.broker_account_id -> the connected account
DELETE /brokers/{id}          (after one order)             500
GET /brokers                  -> still ACTIVE
```

So the endpoint failed for exactly the accounts it matters for: the ones
that have actually traded, which are the ones a user most wants to revoke
after a leaked token or when switching brokers. `BrokerName.PAPER` stamps
its id too, so reaching this needs no real broker credentials at all. And
there is no other endpoint that disables a broker account -- a user could
connect several and disconnect none of them.

The fix is a soft disconnect: mark the row `DISCONNECTED` and scrub its
credentials, rather than deleting it. Every piece of that already existed
and was simply never written to. `BrokerAccountStatus.DISCONNECTED` was a
declared-but-unused enum member (nothing in `app/` assigned it),
`resolve_broker` already selects only `status == ACTIVE` -- falling
through to the next active account or `MockBroker` -- and
`tests/trading/test_broker_resolver.py::test_disconnected_account_is_ignored`
already asserted that behaviour against a status nothing produced.

The two alternatives were both worse. A delete cascade, or
`ON DELETE SET NULL` on the FK, destroys precisely the per-order audit
trail that `broker_account_id` was added to provide.

The credentials are cleared because the hard delete removed them as a side
effect, so a naive soft-delete would have silently regressed the security
posture of an endpoint whose likely trigger is a compromised token.
`encrypt_credentials({})` keeps the NOT NULL column valid, and
`resolve_broker` only ever decrypts an ACTIVE account, so a scrubbed row
is never read back; reconnecting creates a fresh row.

Two properties worth stating. The operation is idempotent -- disconnecting
an already-disconnected account is another 204, not a 404, because the row
is still there. And a trading stack already built for this user in this
process keeps its resolved broker until the process restarts, which is the
same limitation `_stack_for` in `app/api/orders.py` already documents for
connects; this change does not alter that, and closing it would mean
invalidating the in-memory stack cache, which is a separate piece of work.

Coverage was absent: `grep "client.delete"` across the suite hits only
`/replay/{id}`, `/paper/{id}` and three admin kill-switch keys --
`DELETE /brokers/{account_id}` had no test of any kind. The suite's own
cleanup helpers delete orders before broker accounts and *have* to, so the
ordering constraint was understood by the tests and simply never asserted
against the endpoint. The new `tests/api/test_brokers_disconnect.py`
covers the account-with-orders case (asserting first that the order really
does reference the account, so the test cannot pass without exercising the
bug), the status/credential-scrub result, idempotency, and a control that
one user cannot disconnect another's account. Pre-fix the first three fail
with `ForeignKeyViolationError: ... is still referenced from table
"orders"`.

## One positions row per engine, not per instrument (§9, §86)

`positions` is a DB mirror of an in-memory `PositionManager`, and three
unrelated ones write into it:

- the manual/live stack in `app/api/orders.py`'s `_STACKS`, used by both
  `POST /orders` and `POST /options/execute`
- one `PaperTradingEngine` per `POST /paper` session
- `AutoTradeSupervisor`, in the separate worker process

They share no state, and cannot: the supervisor is a different process.
Each also carries its own `MockBroker` with its own balance. Yet all three
persist under `ExecutionMode.PAPER` whenever no broker account is
connected -- which is every account's default, because
`_execution_mode_for` maps a `MockBroker` stack to PAPER -- and
`persist_position` looked its row up by `(user, instrument,
execution_mode, is_open)`. One key, three writers, last write wins.

Driven through the real API as one user with no broker connected:

```
paper session fills            positions: [(151.515152 @ 103.0, open)]
GET /portfolio total_exposure  15606.06
POST /orders (same instrument) 201, quantity 100
positions                      [(100.0 @ 100.0, open)]     <- same row id
GET /portfolio total_exposure  10000.0
```

The paper session's 15,606 did not move, close or merge; it was
overwritten in place and disappeared from `positions`, from
`GET /portfolio`, from `POST /admin/portfolio-snapshot`, and from the
correlated-exposure risk check. That is exactly backwards from why
`persist_position` was called from those paths at all -- both
`app/api/paper.py` and `app/workers/auto_trade_worker.py` carry comments
saying they persist *so that* `GET /portfolio`, the admin snapshot and the
correlated-exposure check can see the position. The reverse ordering is
just as bad: feeding a candle to a paper session whose position has since
flattened writes `quantity=0, is_open=False` over the *manual* position's
row, so `total_exposure` reads 0.0 while the manual stack still holds
stock, and the next manual fill inserts a second row, splitting one
position in two.

Two paper sessions on one instrument collide the same way, with no
`LIVE_TRADE` permission needed at all -- `create_paper_session` has no
guard against a second session on an instrument the user already trades,
and needs none once rows are keyed per session.

The fix is a `source_key` discriminator on `positions`, added by migration
`c93a5f2e10b7`, carried in the lookup key and enforced by a partial unique
index `uq_open_position_per_source` on `(user_id, instrument_id,
execution_mode, source_key) WHERE is_open`. `"manual"` for `POST /orders`
and `POST /options/execute`, `"auto"` for the supervisor, and
`"paper:{session_id}"` per paper session, since two sessions are two
independent engines. `persist_position` takes it as a **required**
keyword-only argument rather than a defaulted one, precisely so a future
caller cannot silently join an existing engine's row -- the mistake this
whole section is about. `compute_portfolio_exposure` needs no change: it
already sums every open row for the mode, so it now reports 25,606.06
where it used to report whichever engine wrote last.

Existing rows are backfilled to `'manual'`. There is no way to recover
which engine produced a historical row, and those rows are unreliable
anyway precisely because they have been overwriting each other; `'manual'`
is chosen because `POST /orders` is the likeliest origin of a surviving
one. The partial index builds safely on existing data, since the old code
overwrote rather than inserted and so could only ever leave one open row
per `(user, instrument, execution_mode)`.

What this does **not** fix, and is worth being plain about: two engines in
two processes still hold independent in-memory position state for the same
account and instrument, and neither knows about the other. Their risk
limits, their `max_open_positions` counts and their broker balances remain
separate. Unifying that is an architecture change, not a bug fix. This
change stops the *database* from silently discarding one of them, so
`/portfolio` and the exposure checks finally see the whole picture.

A related pre-existing split, deliberately left alone: `GET /portfolio`
computes `open_position_count` from the in-memory manual stack while
`total_exposure` comes from the DB, so during the repro above the response
read `open_position_count: 0` alongside `total_exposure: 15606.06`. Both
sources are documented in that endpoint, and reconciling them is a
separate question from the clobber.

Coverage was fake-coverage shape (b) throughout: every existing positions
assertion in the suite is single-writer, and all of them --
`tests/api/test_paper.py`'s persistence test and the eight in
`tests/api/test_orders.py` -- read
`select(Position).where(Position.user_id == ...)` then `.scalar_one()`.
The assertion itself presumes exactly one row, so no fixture in the suite
could reach the collision. The new `tests/api/test_position_sources.py`
drives the paper API and `POST /orders` against one instrument for one
user, two paper sessions against one instrument (with different starting
balances, so the two rows are provably distinct rather than one value
written twice), a control that repeated writes from one source still
upsert a single row rather than accumulating one per candle, and the
partial unique index itself -- including that two *closed* rows for one
source stay legal, which is what lets a source reopen after flattening.

## The autonomous loop analyses stored history, not process uptime (§54)

`AutoTradeSupervisor._process` loads the instrument's whole stored series
and then used one bar of it:

```python
candles = await get_candles(db, instrument.id, strategy.timeframe)
latest = candles[-1]
...
outcome = await engine.on_candle(latest, db)
```

`PaperTradingEngine` -- the engine this supervisor drives -- keeps its own
`self.candles`, starts it empty, appends one bar per `on_candle`, and runs
`smc_engine.analyze(self.candles)` over *that* list. The engine is cached
in `self._engines` and only ever refreshed for `strategy` and
`risk_engine.limits`; nothing seeded its history and nothing else writes
to it. So the analysis window was not the instrument's history -- it was
however long the worker process had been running.

Every sibling passes the full series: `ScannerWorker`, `POST /scanner`,
`GET /charts/{id}/smc`, `POST /backtest`, `POST /replay`,
`POST /ai/analyze`. This was the one component that did not.

Driven through the real supervisor against three NSE sessions of 15m bars
(75 stored), a fresh worker process:

```
DB candle count                 75
engine.candles after one pass    1
PREVIOUS_* liquidity pools       0   (full history: 4)
sweeps detected                  0   (full history: 4)
ScannerWorker, same bar          matched=True
AutoTradeSupervisor, same bar    no signal
```

It is not a warm-up that clears in three bars.
`detect_session_levels` only emits a `PREVIOUS_DAY_*` / `PREVIOUS_WEEK_*`
pool when the candle list it is given spans a bucket boundary, so an
engine built mid-session carries no previous day inside its window at all
and produces zero such pools -- and therefore zero sweeps -- for the rest
of that session, and up to a full trading week for the weekly levels.
`ConditionType.LIQUIDITY_SWEEP` reads exactly those pools via
`smc.recent_sweeps()`, and it is the blueprint's own canonical strategy
example. So for a whole session the scanner writes a `Signal` row and
publishes it on `/ws/signals` while the supervisor -- whose module
docstring says it is "what turns a match into VALIDATE/RISK CHECK/TRADE"
-- silently declines the same strategy on the same instrument at the same
bar.

It moves numbers as well as decisions. `smc.dealing_range` is the stop for
the default `entry.type="market"`, so entry, stop and the
`calculate_position_size` quantity derived from them all came off a window
whose length equalled process uptime.

This is not only a restart concern. A fresh engine is built the first time
a strategy is flipped `eligible_for_auto_trading` via
`PATCH /strategies/{id}/status`, and the first time an instrument becomes
active -- i.e. on the ordinary onboarding path, against instruments that
already have months of stored candles.

The fix seeds a freshly-built engine with `candles[:-1]` (`on_candle`
appends `latest` itself) so its first evaluation sees the same series
every other component sees. Placement matters: the seed goes *after* the
`_last_candle_seen` guard, because an early return there would leave the
engine holding history it had never consumed the final bar of, and the
next pass would append a newer bar over that gap. The condition is
`if not engine.candles`, which is true exactly once per engine.

`POST /paper` is deliberately left alone. A manual paper session starts
empty because the caller drives it with `POST /paper/{id}/candle`; mixing
stored history into a hand-fed session would change what that endpoint
means. The supervisor is the path where the stored series is the
authoritative input and was being discarded.

Not addressed here: `engine.candles` now grows without bound across a long
worker lifetime, and each pass re-runs full SMC analysis over it. That
cost profile is the same one `ScannerWorker` already has (it re-analyses
the full series for every instrument on every pass), so this change makes
the supervisor consistent with its siblings rather than newly expensive --
but bounding the window is a real follow-up, and it should be done for
both components together or neither, since they must analyse the same
thing to agree.

One consequence worth flagging loudly, because this change enlarges it.
`AutoTradeSupervisor.run_once` selects **every active instrument** and runs
every eligible strategy against all of them -- it does not filter on
`StrategyDefinition.market` (the same gap `ScannerWorker` has, recorded
separately). While the supervisor was analysing one-bar windows that was
largely inert: almost nothing matched, whatever the instrument. Now that
it analyses real history, an auto-trading user's strategy will genuinely
fire against every instrument in the system whose data satisfies it, and
because `PositionManager`, `RiskWindow` and the daily-trade counter are
shared per user, unrelated instruments consume that user's
`max_open_positions` and `max_trades_per_day` budget. The behaviour was
always specified this way; this change is what makes it bite. Anyone
running the autonomous loop against a multi-instrument universe should
treat honouring `strategy.market` as a prerequisite rather than a
nice-to-have.

That is not theoretical: it is exactly how it surfaced here. Six existing
`test_auto_trade_worker.py` tests fail against the long-lived development
database, which has accumulated 41 leftover instruments carrying 40 stored
candles each; those instruments now produce signals for the test user's
strategy and exhaust its shared risk budget before the test's own
instrument is reached. The same suite is green on a database created fresh
from the migrations (381 passed), which is what CI uses, and the full
suite passing there rules out any ordering dependency within a single run.
The existing tests are left as they are -- they are correct against a
clean database -- but they call `run_once()`, so they inherit whatever
else lives in the database, and the new tests deliberately drive
`_process` for a single triple instead.

Coverage was fake-coverage shape (b). Every test in
`tests/workers/test_auto_trade_worker.py` inserts exactly one candle and
then calls `run_once()`, in a loop, so the stored history and the engine's
in-memory history are identical by construction and the divergence cannot
occur. Its `SETUP` fixture is also ten bars one minute apart inside a
single morning, so `detect_session_levels` returns `[]` for both windows
and the previous-day path is never exercised at all. The new
`tests/workers/test_auto_trade_history.py` seeds three sessions of stored
bars *before* the supervisor ever runs and asserts the engine analyses all
75; that the seeded window yields previous-day pools and sweeps where the
one-bar window yields neither; that a second pass appends rather than
re-seeding (no duplicated or missing bars); and that an instrument with
two stored bars still declines via the `len(self.candles) < 3` guard
rather than an index error. It drives `_process` directly for one triple
rather than `run_once()`, which would iterate every active instrument in
the database.

## The shared risk window was reset by any instrument whose feed ran behind (§56-57)

Two sections above describe getting the daily/weekly risk counters to
accumulate and then to reset at calendar boundaries. `RiskWindow`
(`app/paper/engine.py`) is where that reset lives for the autonomous
path, and `AutoTradeSupervisor` keeps **one per user**
(`self._risk_windows`, `app/workers/auto_trade_worker.py`) precisely
because `max_trades_per_day`, `daily_loss_limit` and `weekly_loss_limit`
are account-wide: a user trading N instruments across M strategies gets
one engine per triple, and a counter held on an engine would be a
per-triple counter, giving them N*M times the cap they configured.

That sharing is right, but it hands the object a clock it cannot trust.
`PaperTradingEngine.on_candle` rolls the window with `candle.timestamp`
— its logical clock, the same convention `detect_session_levels` uses —
and it does so as its *first* statement, before the open-position early
return and before the `len(self.candles) < 3` guard. So the one shared
window is driven by N different instruments' feeds. Those are not
synchronised with one another, `run_once` selects active instruments with
no `ORDER BY` so the iteration order is arbitrary, and an instrument can
simply be behind: illiquid and yet to print a bar today, mid-backfill, or
halted. Its "latest" candle is then genuinely older than the one another
instrument has already established the window at.

`roll` compared with `!=`:

```python
if self.risk_day is not None and today != self.risk_day:
    self.trades_today = 0
    self.daily_pnl = 0.0
```

which makes a step *backwards* indistinguishable from a new day. One bar
from a lagging instrument zeroed `trades_today` and `daily_pnl` for the
whole account, mid-session, handing back the entire risk budget it had
already spent. Reproduced through `POST /paper/{id}/candle`, driving a
real stop-loss so the engine accumulated the loss itself rather than
having one injected:

```
STEP 1  entry + stop-loss on day D  -> trades_today=1  daily_pnl=-500.00  risk_day=2026-01-05
        daily_loss_pct the gate sees = 0.50% (cap 2.00%)
STEP 2  ONE candle dated D-1        -> trades_today=0  daily_pnl=0.00     risk_day=2026-01-04
        daily_loss_pct the gate sees = 0.00%  <- the halt can no longer fire
```

The fix is not simply `>` in place of `!=`. The marks are now high-water
marks that only ever move forward:

```python
if self.risk_day is None:
    self.risk_day = today
elif today > self.risk_day:
    self.trades_today = 0
    self.daily_pnl = 0.0
    self.risk_day = today
```

Guarding only the reset while still assigning `self.risk_day = today`
unconditionally leaves the mark tracking "the last timestamp seen", so
the lagging instrument rewinds it and the *next* bar from the up-to-date
instrument compares against the rewound value, reads as a fresh day and
resets after all — and with N instruments this alternates, wiping the
counters repeatedly. That variant was written out and run against the new
tests to confirm it: it still fails three of the seven.

`None` continues to mean "no window established yet", so a freshly
constructed window adopts whatever clock it is first shown, in either
direction, without treating it as a boundary crossing.

**Left alone deliberately:** `_UserTradingStack._roll_risk_window`
(`app/api/orders.py`), the sibling this mirrors, keeps its `!=`
comparison. It is fed `datetime.now(timezone.utc)` — one monotonic wall
clock per account, not N instrument feeds — so it cannot exhibit this,
and the two paths' clock disciplines are genuinely different rather than
accidentally divergent. Changing it would be a speculative edit to a path
with no demonstrated failure.

Why the suite could not see this. `tests/workers/test_auto_trade_worker.py::
test_supervisor_caps_trades_per_day_account_wide_across_instruments` is
the one fixture with two instruments sharing a window — and it writes the
*same* `candles[i]` object to both, so both engines always roll with an
identical timestamp and `roll` is a permanent no-op. It varies everything
about the two-instrument case except the one thing that matters. The only
other tests of the reset (`tests/paper/test_engine.py::
test_paper_engine_resets_daily_and_weekly_counters_at_boundaries` and
`tests/api/test_orders.py::test_risk_window_rolls_at_day_and_week_boundaries`)
only ever advance the clock and assert *that* the counters reset; that a
backwards step must **not** reset them is a contract neither states.
`tests/paper/test_risk_window.py` covers it now, including the alternating
lagging/forward sequence and the two-engine shared-window case driven
through `on_candle` itself.

## The payoff window never reached the put side's real extreme (§38-40, §56)

`compute_payoff_summary` (`app/options/payoff.py`) decides whether a
strategy's profit or loss is bounded by looking at whether the payoff curve
is still sloping at the edge of a sampled price range. The default range ran
from `min(strikes) * 0.5` to `max(strikes) * 1.5`.

The upper end is right: a call's loss really does run away as the underlying
rises, with nothing to stop it. The lower end is not. An underlying cannot
trade below zero, so price 0 is a *domain boundary*, not a truncation, and it
is exactly where the put side of any combination reaches its extreme — a
short put's maximum loss, a long put's maximum profit — both finite and
computable in closed form. Sampling only down to half the lowest strike
stopped short of that point while the curve was still falling, so the edge
test concluded "unbounded" for outcomes that are bounded, and every figure
derived from the sampled minimum came back roughly half its real size.

That matters because `app/risk/options_risk.py` sizes the strategy from
exactly these fields:

```python
risk_amount = (
    abs(payoff.max_loss)
    if payoff.max_loss is not None
    else max(-payoff.worst_sampled_loss, payoff.capital_requirement)
)
```

A naked short put — the plainest short-volatility position there is, and one
`POST /options/execute` accepts directly, since it takes an arbitrary list of
legs with arbitrary direction — therefore fell into the `None` branch and was
sized by a number drawn from half the strike. Measured on a 1000-strike put
sold at 100 with a lot size of 150, against the 100,000 mock balance:

```
max_loss reported by the payoff engine : None
risk_amount the gate actually used     : 60,000   -> exposure  60.00% vs limit 100%  APPROVE
TRUE max loss at underlying = 0        : 135,000  -> exposure 135.00% vs limit 100%  REJECT
```

`POST /options/execute` returned 201 and the leg went to the broker. The
account was allowed to sell a put that can lose more than its entire balance,
because the control that exists to prevent that was shown 44% of the real
number. Note this is a different fault from the earlier options-exposure fix
recorded above: that one corrected *which field* the gate reads, and left the
sampling window that produces those fields unexamined.

The fix is in two parts, and the second is not optional.

**Sample from 0.** The default range now starts at the real floor of an
underlying price, so the put side's extreme is inside the window and comes
out exact rather than inferred.

**Settle boundedness from the edge slope alone.** The original test also
required the sampled extreme to *sit* on the sloping edge
(`right_slope < 0 and max_loss_sample == payoffs[-1]`). Moving the floor to
zero moves where the sampled minimum sits: for a short straddle the worst
sampled point becomes the left edge, not the right, and that conjunct stops
firing — silently re-labelling a genuinely unlimited-loss position as
defined-risk. The first cut of this fix did exactly that, and the control
test caught it before it shipped. The conjunct was never the right test: a
payoff at expiry is piecewise linear with kinks only at strikes, so past the
outermost strike the slope is constant forever, and a still-falling right
edge keeps falling regardless of where the sampled minimum happens to be.
The left-edge inference is kept for an explicitly supplied `price_range`
that does not reach zero, since such a window genuinely is truncated.

Verified across the matrix: short put, long put and defined-risk spreads
report exact bounded figures; naked short call, short straddle and short
strangle still report `max_loss=None`; long call and long straddle still
report `max_profit=None`. `worst_sampled_loss` for a genuinely unbounded
combination also improves — a short straddle's now reaches its true −120,000
floor at zero instead of the −45,000 that happened to sit at the old right
edge — which tightens the fallback sizing path for unbounded strategies
without changing what that field means.

Why the suite could not see this. Every fixture in
`tests/options/test_payoff.py` is call-side or fully defined-risk
(`long_call`, `bull_call_spread`, `bear_call_spread`, `iron_condor`); not one
carries an uncovered short put. The "unbounded" fixture in
`tests/risk/test_options_risk.py` is `_synthetic_short` (long put + short
call), whose unboundedness comes from the *call* leg running away to the
right, so the put-side truncation is never what an assertion turns on. This
is fake-coverage shape (b) again: a fixture that structurally cannot reach
the breaking state. `tests/options/test_payoff_put_side.py` covers it now,
including the unbounded-loss controls that pin the second half of the fix.

## Previous-day/week levels could not be swept by the session's opening bar (§22)

`detect_sweeps` (`app/smc/liquidity.py`) scanned `range(pool.formed_index + 1,
len(candles))` for every pool it was handed. But `formed_index` does not mean
the same thing for the two kinds of pool that reach it, and the two writers
disagree with the one reader:

* `detect_equal_levels` anchors a pool at its **last member swing**. That
  candle helped form the level, so it must be excluded — otherwise the pool
  sweeps itself, which is exactly the fault recorded in the equal-levels
  section above.
* `detect_session_levels` anchors a previous-day/week level at the **first
  candle of the following period**, which its own docstring describes as
  "when the level becomes a resting liquidity target rather than an
  in-progress extreme". `period_high` and `period_low` are reset only *after*
  the pool is emitted, so that candle contributed nothing to the level it is
  being measured against, and must be included.

Applying the equal-levels rule to both meant every PREVIOUS_DAY_HIGH/LOW and
PREVIOUS_WEEK_HIGH/LOW pool skipped the single candle most likely to raid the
prior session's extreme — the opening bar.

Two measured consequences, on 15m bars with day 1 spanning 99..110:

```
day 2 bar 0 = (o=104 h=115 l=103 c=106)   -- 5 points through the PDH, closes below
  PREVIOUS_DAY_HIGH price=110 formed_index=3  swept=False  swept_index=None
  SMCContext.recent_sweeps() -> []
```

The raid is lost outright. And where a later bar also trades through the
level, the sweep is recorded against the wrong candle:

```
day 2 highs [115, 109, 111]  -- the genuine raid is bar 3 (115), bar 5 only nicks 111
  swept_index=5   (two bars, 30 minutes late; 1 point through instead of 5)
```

`swept_index` is not cosmetic. `app/strategy/evaluator.py`'s
`ConditionType.LIQUIDITY_SWEEP` gates on `current_index - p.swept_index <
condition.lookback`, so a late index shifts the whole window in which a
strategy may act on the sweep, and `rejected` is decided by a different bar's
close. These pools are produced by `SMCEngine.analyze` under default config,
so the effect reaches `ScannerWorker` (persisted `Signal` rows and
`/ws/signals`), `POST /scanner`, `BacktestEngine`, `PaperTradingEngine`,
`AutoTradeSupervisor` and the `GET /charts/{id}/smc` overlay. The setup it
silently suppresses is the platform's headline one: a judas swing that takes
out the previous-day high on the opening bar and reverses.

The fix scans from `formed_index` and skips `pool.member_indices`. That
states the invariant the `+ 1` was standing in for — *a pool cannot be swept
by the swings that define it* — rather than a positional proxy that happens
to coincide for one pool kind. Equal-level pools are unaffected byte for
byte: `formed_index` is their last member, so it is skipped either way.
Session-level pools carry no members at all, so their anchoring bar is now
examined.

The anchor itself was deliberately left alone. Moving
`detect_session_levels` to `formed_index = i - 1` would also have made the
`+ 1` land correctly, but `formed_index` and `formed_timestamp` carry a
separate meaning — when the level came into existence — and `i - 1` is the
last bar of the *previous* period, which would falsify both and contradict
the docstring. The disagreement was in the reader, so the reader is where it
is resolved.

Why the suite could not see this. Every sweep assertion in
`tests/smc/test_liquidity.py` builds candles with `conftest.make_candles`,
which emits them one minute apart from a single `2026-01-05 09:15Z` start;
the two fixtures span 8 and 9 minutes on one date, so `detect_session_levels`
returns `[]` for both "day" and "week" and only equal-level pools are ever
swept. `detect_session_levels` had no direct test anywhere in the suite —
its only mentions in `tests/` were comments in
`tests/workers/test_auto_trade_history.py` and `tests/market/test_aggregation.py`
noting that it returns `[]`. All 16 `tests/smc` tests passed against the
broken code. Fake-coverage shape (b) again: a fixture that structurally
cannot reach the breaking state.
`tests/smc/test_session_level_sweeps.py` covers it now, including a control
pinning that session pools never populate `member_indices` — the property the
fix depends on.

## `order_block_retest` was unfillable by construction (§24)

`OrderBlock.mitigated` had two incompatible meanings. The writer,
`app/smc/order_blocks.py::update_mitigation`, set it on the first candle whose
range overlapped the block at all:

```python
if candle.low <= block.top and candle.high >= block.bottom:
    block.mitigated = True
```

Every reader takes it to mean *still available to trade into*.
`SMCContext.active_order_blocks` filters on `not mitigated`, and through that
filter it drives `ConditionType.ORDER_BLOCK` (`app/strategy/evaluator.py`),
the `order_block_retest` entry (`app/strategy/engine.py`), the AI context
(`app/ai/context_builder.py`) and the chart overlay (`app/api/charts.py`).

For a retest entry those two readings are not merely different, they are
mutually exclusive. The entry is the block's midpoint, so
`bottom <= entry <= top`. The fill gate both execution engines apply is
`candle.low <= entry <= candle.high`. Together those force `candle.low <= top`
and `candle.high >= bottom` — precisely the old mitigation test. And
`SMCEngine.analyze` re-runs `detect_order_blocks` (which calls
`update_mitigation` over the whole visible window, current bar included)
*before* the strategy is evaluated. So the bar that could fill the retest was
always the bar that had just removed the block from `active_order_blocks()`.
The only exempt bar is `caused_event_index` itself, because the scan starts at
`caused_event_index + 1` — meaning the entry could only ever fill on the
structure-break candle, never on the retest it is named for.

In practice it died even earlier than that. Measured on the suite's own swing
fixture, which leaves an unmitigated bearish block at `[102, 107]`
(midpoint 104.5) created by the break at bar 10, with a rally back into it:

```
bar | range        | active blocks | entry | fills
 11 | ( 95.0, 99.0) |      1       | 104.5 | no
 12 | ( 97.0,102.0) |      0       | None  | no   <- merely grazes the 102 edge
 13 | (100.0,106.0) |      0       | None  | no   <- the real retest, through 104.5
```

Bar 12 never reaches the midpoint; it only touches the block's lower edge, and
that alone retired the zone a full bar before the setup triggered. On the same
candles `BacktestEngine` booked **0 trades** for `order_block_retest` and **1
trade** for `fvg_retest` — the same strategy shape, the same data, differing
only in which zone type it keys on.

The harm is not confined to backtests. `POST /scanner` and `ScannerWorker`
apply no fill gate, so they do the inverse: they publish and persist a
`Signal` naming `entry=104.5` on the bars where price is nowhere near it, then
fall silent on the bar where it is actionable. The signal feed was live
exactly when it was useless.

The fix gives `OrderBlock` the graded fill its sibling already had.
`app/smc/fvg.py::update_mitigation` accumulates a `filled_percentage` and only
sets `mitigated` at full fill or on invalidation; order blocks now do the
same, with the same direction convention — a bullish block is demand below
price and fills downward from its top, a bearish block is supply above price
and fills upward from its bottom. On the fixture above the retest bar grades
0.8 and the block stays tradable, so the entry resolves and fills at 104.5;
a later candle that trades fully through it, or engulfs it outright, still
retires it.

This closes a divergence rather than inventing a convention: two zone types
that mean the same thing to every consumer were being retired by two
different rules, and only one of them was compatible with how the entry
resolution and fill gate work.

One thing worth recording because it reads as a warning sign rather than a
problem: `tests/workers/test_auto_trade_worker.py` uses `order_block_retest`
as its `NEVER_MATCHING_DEFINITION`. That helper still never matches — its real
gate is a *bearish* order-block condition against bullish fixture data, which
produces no bearish blocks at all, verified directly — but the codebase having
reached for this entry type as a reliable way to never fire is a fair summary
of the defect.

Why the suite could not see this. The two tests that look like retest
coverage — `tests/backtest/test_engine.py::
test_backtest_only_fills_a_retest_entry_once_price_actually_trades_there` and
`tests/paper/test_engine.py::
test_a_retest_entry_fills_at_its_level_not_at_the_candle_close` — assert the
generic "a retest entry" contract, and the comments they guard name *both*
retest types, but both fixtures use `EntryConfig(type="fvg_retest")` only.
Because FVG mitigation is graded, a midpoint touch there is a partial fill and
the gap stays active, so those fixtures structurally cannot reach the
order-block branch. `tests/smc/test_order_blocks.py` had a single test that
never touched `mitigated`. And `order_block_retest` had no positive-path
coverage anywhere: its only other appearance under `tests/` is the
never-matching definition above. Shapes (b) and (f) together.
`tests/smc/test_order_block_mitigation.py` covers it now.

## Options contracts could not be registered through the API (§37-40, §120)

`instruments` is the only table an options contract can live in, and
`POST /instruments` is the only route that creates one — there is no PUT,
PATCH or DELETE on instruments, and `Instrument(**payload.model_dump())` in
`app/api/markets.py` is the only `Instrument(...)` construction anywhere in
`app/`.

`InstrumentCreateRequest` carried none of `underlying`, `expiry`, `strike` or
`option_type`, though all four are real columns on the model. Pydantic's
default `extra="ignore"` therefore discarded them from a caller's body
without complaint, and `InstrumentResponse` did not expose them either, so
the loss could not be observed from outside. Measured end to end against a
database built fresh from migrations:

```
POST /instruments      -> 201
  sent          : underlying, expiry, strike and option_type all supplied
  response keys : active, exchange, id, instrument_type, lot_size, market,
                  symbol, tick_size          <- none of the four
  DB row        : market=OPTIONS underlying=None expiry=None strike=None option_type=None
POST /options/execute  -> 422  "NIFTY...CE is not an options contract"
```

The write succeeded, four supplied fields vanished, and the row was
permanently unusable: `app/api/options.py` rejects any leg whose instrument
has no `option_type` or `strike`, and no endpoint can repair an existing row.
Multi-leg options execution — the whole `app/options/` and
`app/risk/options_risk.py` stack, the combined-payoff risk gate, the
liquidity filter and the per-leg order pipeline, all of which several earlier
sections above describe hardening — was unreachable through the API for every
instrument the API itself could create. Reaching it at all required bypassing
the API and inserting rows with SQL.

The fix adds the four fields to both the request and the response, and
validates on write the invariant `app/api/options.py` enforces on read: a
`market=OPTIONS` row must carry `strike` and `option_type`. The mirror rule is
enforced too — a non-options instrument may not carry them, since an EQUITY
row with an `option_type` would read as an option to anything that checks
`is not None` later. After the fix the same script registers the contract and
`POST /options/execute` returns 201.

**`expiry` is deliberately not required.** Nothing in `app/` reads
`Instrument.expiry` today — grep finds no consumer — so demanding it would be
inventing a constraint nothing depends on. It is carried and returned, and
should become required alongside options-chain ingestion, when something
finally prices against it. `underlying` is in the same position.

Why the suite could not see this, and why it is the sharpest example of
fixture-shaped blindness in this codebase. Two test files each look like
coverage and neither touches the defect:

* `tests/api/test_instruments.py` is *about* this endpoint, with a module
  docstring on its contract — but its shared `_payload()` helper hardcodes
  `market="EQUITY", instrument_type="EQ"` and takes only a symbol, so no test
  ever sent `market="OPTIONS"` to this route.
* `tests/api/test_options_execute.py` is *about* options execution — but
  `_make_two_leg_instruments` builds `Instrument(...)` ORM rows directly
  through `async_session_factory()` with `strike` and `option_type` already
  filled in, and commits them. It never calls the endpoint.

So the suite proved the execute pipeline works on rows the API could not
produce, while the endpoint's own tests proved it works for the one market
type that needs none of the missing fields. `market="OPTIONS"` was a
user-selectable enum value with no test exercising it on the only route that
can create one. `docs/PRODUCTION_READINESS.md` has been corrected: its
"Options execution tested" row claimed the multi-leg path was real and tested
and disclosed only the missing chain ingestion, which was an overclaim of
exactly this shape.

## Deleting a paper session orphaned its mirrored position row (§49, §86)

`feed_candle` mirrors a paper engine's position into Postgres on every candle
under `source_key=f"paper:{session_id}"`, `execution_mode=PAPER`,
`is_open=True` — the fix recorded two sections above, so that a large open
paper position is visible to `GET /portfolio` rather than appearing only once
it closes.

`close_paper_session` took no `db` dependency at all. Its whole body was an
ownership check and `del _SESSIONS[session_id]`, and its docstring claimed
that "only the live working copy and any still-open position's unrealized
state are discarded". The open `positions` row was the part that wasn't.

Nothing else could clean it up either. `app/trading/persistence.py` is the
only writer of `Position.is_open` anywhere in `app/`, and reaching it needs a
live engine *and* the session's `source_key` — both of which this endpoint is
in the act of destroying. A new session gets a new UUID and therefore a new
key, so the row became permanently unaddressable.

`app/risk/portfolio.py::compute_portfolio_exposure` sums exactly those rows.
Measured on a fresh database, creating a session, feeding it until a LONG
opened, and deleting it three times for one user on one instrument:

```
after delete #1: total_exposure=15606.06   open_position_count=0
after delete #2: total_exposure=31212.12   open_position_count=0
after delete #3: total_exposure=46818.18   open_position_count=0

positions: 3 rows, all is_open=True, qty=151.5152 @ 103.0
GET /paper/{id} -> 404 for all three
```

The account held nothing. Exposure grew by a whole position per cycle and
never came back down, and `portfolio_snapshots` journaled the same inflation.
The same orphaning happens to every open paper session lost to an API
restart, since `_SESSIONS` is in-memory — that half belongs to the broader
documented in-memory limitation and is not addressed here.

**This was a reporting fault, not a control failure.** `RiskEngine`'s
`current_exposure` on both `POST /orders` and `POST /options/execute` is
summed from the in-memory `PositionManager`, never from this table, so
nothing failed open — the number shown and journaled was wrong, but no gate
was bypassed. (`open_position_count` reading 0 throughout is the separate,
still-deferred memory-vs-DB split in `GET /portfolio`.)

The endpoint now retires the mirror before dropping the session, through a
new `abandon_position_mirror` in `app/trading/persistence.py`. It needs to be
a distinct function rather than a `persist_position` call: that upsert
derives `is_open` from a `PositionRecord`, whose `is_open` is the property
`quantity != 0`, so there is no way to hand it "flat but never filled". The
lookup is keyed identically to `persist_position`'s, so it can only ever
retire the row this source itself wrote — deleting one session leaves a
second session's still-open row untouched.

Two deliberate choices about what *not* to do:

* **No `Trade` is journaled.** Nothing was sold at any price; the simulation
  was abandoned. `GET /portfolio.total_realized_pnl` sums the `trades`
  journal, so inventing an exit here would put a fabricated P&L into the
  account's realized total — a worse fault than the one being fixed.
* **The row is retired, not deleted,** and keeps its quantity and average
  price. `unrealized_pnl` is zeroed because an abandoned position has no live
  mark. What the discarded session held stays on the record; only its
  contribution to open exposure goes away.

Why the suite could not see this. `tests/api/test_paper.py::
test_create_get_feed_and_close_paper_session` is the test whose *name*
asserts this exact contract, but its fixture feeds a single candle and
`PaperTradingEngine.on_candle` cannot produce a signal from one bar — so no
`positions` row is ever written and there is nothing to orphan. It asserts
only 204 then 404. Its sibling `test_paper_trading_persists_the_open_position
_to_the_database` does reach an open row and does assert `is_open is False`,
but only via the natural target exit; it never deletes while the position is
open. `tests/api/test_portfolio.py::test_portfolio_reports_real_exposure_for_a
_paper_account` goes through `POST /orders` (`source_key="manual"`), never a
paper session. No test in the suite called DELETE with a position open.
Shapes (b) and (e) together, in the one test named for the contract.

## Cancelling a SUBMITTED order was dead by construction (§57, §101)

`POST /orders/{id}/cancel` decides which statuses may be cancelled:

```python
if order.status not in (OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED):
    raise HTTPException(409, ...)
```

`app.trading.order_manager._ALLOWED_TRANSITIONS` decides which may actually
move to `CANCELLED`, and its `SUBMITTED` row was
`{ACKNOWLEDGED, REJECTED, FAILED}`. The two halves of one contract disagreed
about the same status, so every cancel of a SUBMITTED order raised
`IllegalTransitionError` at the transition line — which the catch-all handler
in `app/main.py` turns into a 500. Nothing below that line ran: no
`persist_order`, no `record_audit`, no websocket publish, and the order's
status did not move. Measured end to end through the API against a wedged
order:

```
before: POST /orders/{id}/cancel -> 500,  status after: SUBMITTED
after : POST /orders/{id}/cancel -> 200,  status after: CANCELLED
```

SUBMITTED is also the state most in need of cancelling.
`ExecutionEngine.submit` transitions to SUBMITTED and *then* awaits
`broker.place_order`; if that call raises, the order rests there forever.
`UpstoxBroker.place_order` converts `httpx.HTTPStatusError` and `BrokerError`
into a REJECTED result — the adapter's own comment explains that letting a
`BrokerError` propagate would "permanently wedge the order at SUBMITTED: any
retry with the same order params returns `created=False` and never calls
`submit()` again" — but an ordinary transport failure against its 10-second
client (`ConnectError`, `ReadTimeout`, `RemoteProtocolError`, `PoolTimeout`)
is not caught and still propagates. That earlier fix closed one shape of the
wedge and left the other open.

Such an order is in the worst possible state. It exists only in the API
process's `OrderManager`: `place_order` aborted before `persist_order`, so
there is no `orders` row at all — `GET /orders` shows it, `GET /admin/orders`
does not. It has no `broker_order_id`, so `reconcile_orders` reports it as
"SUBMITTED locally but was never submitted to the broker" and
`ReconciliationWorker` halts the account; an admin
`POST /admin/accounts/{id}/resume` is undone by the next pass, indefinitely,
and new entries stay 423-blocked for that account the whole time. The one
escape hatch was `POST /orders/{id}/cancel`, and it was the single thing that
could not work. `app/api/orders.py` is the only caller in `app/` that
transitions to `CANCELLED` at all, so nothing else could clear it either.

The fix adds `CANCELLED` to the `SUBMITTED` row, making the table agree with
the contract the endpoint already advertised. This is LATENT rather than
live: `MockBroker.place_order` cannot raise and `DhanBroker` fails earlier
(`get_quote`/`get_account` raise `NotImplementedError` before an order is
created), so it needs a connected Upstox account plus a transport-level
failure — the canonical broker failure mode, on the one path that handles
real money.

**The mirror mismatch is deliberately left alone.** The table permits
`PARTIALLY_FILLED → CANCELLED`, but the endpoint's guard refuses
PARTIALLY_FILLED with a 409, so that entry is reachable from no caller. That
asymmetry harms nothing — a table edge nobody takes — and admitting it would
be a decision about partial-fill semantics rather than a repair: cancelling
there retires the order while the filled portion remains a real open
position, and nothing on this path reconciles the remainder. Fixing the half
that crashes and recording the half that merely sits idle is the honest split.

The regression test pins the *relationship* rather than the instance:
`tests/trading/test_cancel_transitions.py` parametrises over the statuses the
endpoint admits and asserts each can reach `CANCELLED`, so widening either
half without the other fails immediately.

Why the suite could not see this.
`tests/api/test_orders.py::test_cancel_order_broker_failure_is_surfaced_cleanly_and_leaves_status_unchanged`
is the only test of this endpoint and its docstring claims to cover "a still
cancelable order" — but its `_NeverFillsBroker` *returns* an ACKNOWLEDGED
result rather than raising, and `ExecutionEngine.submit` unconditionally
transitions to ACKNOWLEDGED anyway, which the table always permitted. No test
double anywhere in the suite raises from `place_order`, so no fixture could
produce an order resting at SUBMITTED, and the SUBMITTED row of
`_ALLOWED_TRANSITIONS` had no test of its own. Shape (b) again, with the
fixture's own docstring naming the contract it could not reach.

## Six condition types were dead by construction (§77, §82-83)

`app/strategy/evaluator.py` routes six of the fifteen `ConditionType` members
— `volume`, `volatility`, `indicator`, `options_iv`, `options_oi`,
`options_greeks` — to the same two lines:

```python
key = condition.name or condition.type.value
return _numeric_compare(context.indicators.get(key), condition)
```

`EvaluationContext.indicators` has **no writer anywhere in `app/`**. All five
construction sites (`app/backtest/engine.py`, `app/paper/engine.py`,
`app/api/ai.py`, `app/api/scanner.py`, `app/workers/scanner_worker.py`) omit
it, so the bag is always `{}`, `.get` always returns `None`, and
`_numeric_compare` returns `False` on its first line. Its only other mention
is `app/ai/context_builder.py`, which serializes the empty dict back out.

Because conditions AND implicitly, one such condition zeroes the whole
strategy. Measured against the suite's own pinned setup, on bars carrying
`volume=100.0`:

```
fvg only                          -> matched=True
fvg + volume > 0                  -> matched=False
fvg + volatility < 1e9            -> matched=False
fvg + indicator rsi within ±1e9   -> matched=False
```

Every added condition there is *vacuously true*, and each one silently kills
a strategy that was producing signals a moment earlier.

Nothing surfaces why. `ScannerWorker` and `AutoTradeSupervisor` re-evaluate
such a strategy every 60 seconds forever with `matched=False`, no log and no
error. `POST /backtest` runs the identical evaluator over the identical
context shape, so a validation backtest reports zero trades —
indistinguishable from "this history contained no setups" — which means
blueprint §77's graduation path (Backtest → Out-of-sample → Replay → Paper →
`eligible_for_auto_trading`) cannot catch it at any stage.

The codebase had already ruled on this exact failure mode, twice.
`Condition._reject_unimplemented_boolean_operators` refuses `AND`/`OR`/`NOT`
because a condition using one "silently fell through to `return False` ... on
every single candle forever — a strategy that can structurally never fire,
with nothing anywhere indicating why", and `EntryConfig._reject_unknown_entry_types`
refuses unknown entry types for the same reason. The worked example in the
first of those write-ups, a few sections above, is itself
`type=indicator, name=rsi, operator=NOT` — rejected for its *operator* while
its type went unexamined. Change `NOT` to `LESS_THAN` and the strategy still
could never fire.

So this applies the established ruling to the types themselves. A validator
on `Condition.type` refuses all six, naming the structural types that do
work. Rejecting is not a claim that these conditions are unwanted: `volume`
and `volatility` are computable from the candle series every caller already
holds, `indicator` needs a library, and `options_*` additionally needs the
option-chain ingestion the readiness doc records as missing. Populating
`indicators` at those five sites is the other way to close this, and is what
should replace this validator when the data exists — a regression test pins
that the bag is still empty, so it fails the day a real writer appears.

Already-stored strategies degrade safely rather than crashing: both worker
read paths (`auto_trade_worker.py`, `scanner_worker.py`) already wrap
`StrategyDefinition.model_validate` in `try/except`, log, and skip. A stored
strategy using one of these types therefore moves from *silently never
matching, forever, with no signal* to *skipped with a logged exception naming
the strategy* — strictly better. (No such strategy exists in the development
database; the count is zero.)

**The score denominator is fixed in the same change, because this fix makes
it permanently wrong otherwise.** `app/strategy/scoring.py` reserved
`"volatility": 10.0` of a 100-point `DEFAULT_WEIGHTS` total, and
`compute_strategy_score` divides by `sum(weights.values())`. With
`ConditionType.VOLATILITY` unable to appear in `satisfied_condition_types`,
the reachable ceiling was 90.0 — a strategy satisfying every condition type it
*could* satisfy, at target R, scored 90 and nothing could ever score higher.
Dropping the key makes the denominator the reachable maximum, so that
strategy now scores 100.0. Absolute scores change; ranking does not, since
every score was divided by the same inflated total. The scoring branch itself
is kept and commented, so restoring the component is one weight key rather
than re-derived logic.

Why the suite could not see this.
`tests/strategy/test_dsl.py::test_condition_accepts_implemented_operators` is
the test that *looks* like coverage — it builds a `ConditionType.INDICATOR`
condition — but it only asserts `condition.operator == operator`, both sides
of which come from the same parametrised value, and it never evaluates the
condition. Both shared context helpers (`test_engine.py::_build_context`,
`test_evaluator.py::_context`) construct `EvaluationContext` without
`indicators`, exactly like the production sites, so no fixture in the suite
could produce a non-empty bag. The six types had no evaluation test of any
kind, `_numeric_compare` had no direct test, and `compute_strategy_score` and
`DEFAULT_WEIGHTS` had no test at all — which is why a 10-point unearnable
weight sat unnoticed. Shapes (a), (b) and (f) together. The two operator
tests have been retargeted to a live condition type so each proves the one
thing it names.

## The AI proposal validator trusted the AI's output shape (§81, §110, §131)

`app/ai/validation.py` exists because the AI is not trusted: §131 "AI ≠ Final
Authority", §32, §81 "AI Output Validation". Every *value* the model stated
was cross-checked against the deterministic `StrategyEvaluationResult`. The
*shape* of what it sent back was not checked at all.

`AIClient.complete_json` is annotated `-> dict`, but the only thing the
Anthropic adapter actually guarantees is `json.loads` succeeding
(`_extract_json` in `app/ai/providers/anthropic_client.py`). A bare array, a
string, a number, or an object whose `entry` is the text `"N/A"` are all
valid JSON, and nothing between the model and the validator narrowed that.
The validator then did:

```python
proposed_direction = str(proposal.get("direction", "")).lower()
...
if entry is None or not _within_tolerance(float(entry), deterministic_result.entry):
```

Measured against the module's own pinned matched setup:

```
{"entry": "N/A"}          -> ValueError: could not convert string to float: 'N/A'
{"entry": "1,250.50"}     -> ValueError: could not convert string to float: '1,250.50'
{"entry": {"value": 100}} -> TypeError: float() argument must be a string or a real number
{"risk_reward": "2:1"}    -> ValueError
{"risk_percent": "0.5%"}  -> ValueError
["no trade"]              -> AttributeError: 'list' object has no attribute 'get'
"NO_TRADE"                -> AttributeError: 'str' object has no attribute 'get'
```

None of those are exotic for an LLM writing prose numbers, and every one of
them escaped as an exception from a function whose entire job is to *return*
validation errors.

Where it escaped to matters. `POST /ai/propose-trade` wraps the provider call
in a try/except that records an `AIDecision` row for a failed or unparseable
response — its docstring promises that audit row (§71, §79: "you can't
evaluate AI behavior over time without a record of what it actually said").
`validate_ai_trade_proposal` runs *after* that try/except and *before* the row
is written, so an exception from it skipped the audit entirely. Measured end
to end against a clean database, with the AI returning
`{"decision": "NO_TRADE", "entry": "N/A", ...}`:

```
before:  HTTP 500,  AIDecision rows written: 0
after:   HTTP 200,  AIDecision rows written: 1
         valid=false, errors=["AI decision 'NO_TRADE' is not 'TRADE' — the AI
         did not propose this trade", "Proposed entry 'N/A' is not a number", ...]
```

This is the third instance of the same audit hole in this one endpoint: a
failing provider call and unparseable content were both closed earlier; the
*parsed but malformed* payload was the half nobody had reached, because no
test had ever fed the validator anything but well-formed floats.

The second half of the finding is `decision`. `_TRADE_PROPOSAL_SYSTEM_PROMPT`
demands `"decision": "TRADE"` or `"NO_TRADE"` — it is the one field in the
response carrying the model's own verdict — and grepping `app/` for a reader
finds none. Only the prompt writes about it. So a model that declined the
setup while dutifully echoing the deterministic entry/stop/RR it was
instructed to match came back `valid: true`: in §87 "Assisted" mode, a refusal
presented to the operator as a validated proposal. The validator now reads it,
and treats an absent verdict the same as a decline — a response that never
states one is not an endorsement (§110 "no AI → no trade").

The fix keeps every coercion inside the validator, where a bad value becomes a
named error instead of a stack trace: `_as_float` rejects booleans (no
`float(True) == 1.0` riding into a price) and non-finite values, a numeric
string like `"100.1"` is still accepted so the check isn't stricter than the
model needs, and a non-object response is one error rather than seven.

**What this does not change.** Nothing else validates AI output shape:
`POST /ai/chat` coerces its reply with `str(...)` and is unaffected, and
`app/ai/strategy_builder.py` runs the response through the Strategy DSL
schema, which already rejects malformed content. `EvaluationContext.indicators`
is still unpopulated at this endpoint's construction site, so the structured
facts handed to the model still carry an empty indicator bag.

## The rejection the validator produced could not be delivered (§81, §110)

The section above closed the validator's side: `validate_ai_trade_proposal`
now turns a response that isn't a JSON object into
`AIValidationResult(valid=False, errors=["AI response was not a JSON object:
list"])` instead of raising `AttributeError`. That fix was real but it was
only half the path. `ProposeTradeResponse` still declared:

```python
    proposal: dict
```

So the endpoint computed the verdict correctly, committed the `AIDecision`
audit row, and then raised `pydantic.ValidationError` constructing its own
response — falling through to the catch-all handler as a **500**. The one
error string the new validator branch exists to produce could not be
observed by any API caller.

Wrapping the object in a list is a routine LLM formatting slip, and
`_extract_json` passes through whatever `json.loads` returned. Measured end
to end against a clean database, with the AI answering
`[{"decision": "TRADE", "entry": 100.0, ...}]`:

```
before:  HTTP 500  (ValidationError: proposal - Input should be a valid dictionary)
after:   HTTP 200  {"valid": false,
                    "errors": ["AI response was not a JSON object: list"],
                    "proposal": [{...}], "decision_id": "beff963f-..."}
```

Identical results for a bare string, a bare number and `null` (`str`, `int`,
`NoneType`). The audit row survives in every case — it is committed before
the response is built — so the damage is narrower than the crash the
previous section describes: what the caller loses is the structured
rejection the endpoint's contract promises, and the `decision_id` of the row
that was just written, leaving a recorded decision the client cannot
reference.

`proposal` is now `Any`: it echoes verbatim whatever the model returned, the
same value already stored in `AIDecision.output` (a `JSON` column, which
accepts all of these). Narrowing it to `dict` was the assumption under test.

The sibling endpoint in the same file had the guard all along —
`POST /ai/chat` reads `str(response.get("reply", "")) if isinstance(response,
dict) else str(response)` — which is what made this an asymmetry rather than
an open question: the same untrusted payload, two consumers, one control.

**Why the suite missed it.** `_FakeAIClient.__init__` in
`tests/api/test_ai_propose_trade.py` was annotated `response: dict` and every
test passed a dict literal, so the fixture structurally could not reach a
non-object response (blind-spot shape (b) again). The unit tests added with
the previous fix *do* cover the non-object case, but call the validator
directly — they asserted the branch worked while its only production caller
could not deliver its result. The fake client is now typed `object`, the
annotation that was the assumption in the first place.

## "Device tracking" tracked nothing: `device_info` had no writer (§69)

`UserSession.device_info` is `String(500)`. `_issue_tokens` has taken a
`device_info` argument since the first commit, `refresh()` faithfully
carries it across every token rotation, `SessionResponse` exposes it, and
`GET /auth/sessions` returns it. Every layer was in place except one:

```python
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    access_token, refresh_token = await auth_service.login(db, payload.email, payload.password)
```

`POST /auth/login` took no `Request` and called the service with three
positional arguments, so `device_info` fell back to its `None` default —
on every login, of every user, forever. `UserSession(...)` is constructed
in exactly one place in the repository, so there was no other path by
which a non-`NULL` value could reach that column, and `grep -rn
"user_agent" app/` returned nothing at all.

Two logins from genuinely different devices, measured end to end:

```
User-Agent: Mozilla/5.0 (Macintosh) Chrome/120.0.0.0 Safari/537.36
User-Agent: SuperTradingSystem-Android/1.4 (Pixel 8)

GET /auth/sessions ->
   device_info=None  id=729b7f7d
   device_info=None  id=63611e8e
```

The SQL log shows both `INSERT INTO sessions` statements binding `None`
for `device_info`. A user with five live sessions saw five identical rows,
which makes `POST /auth/sessions/{id}/revoke` unusable for the thing it
exists to do: pick out the session that isn't yours. That endpoint, the
reuse-detection revoke-everything path in `refresh()`, and this column were
all added together as §69's account-security story; the column being empty
is what left that story half-built.

The section above on logout and session management says `device_info` "was
written at every login and never read back by anything — collected, but
write-only". That was the inverse of the truth, and the docstring on
`list_sessions` repeated it ("has been collected at login since the
beginning, but nothing ever read it back"). The reader was the part that
existed. Both have been corrected.

`POST /auth/login` now takes the `Request` and stores the caller's
User-Agent. Two details are load-bearing:

- **It is truncated to 500 characters.** A User-Agent is unbounded
  client-supplied text and Postgres does not silently truncate into a
  `VARCHAR(n)` — measured, a 500-character value inserts and a
  501-character value raises `StringDataRightTruncation`. Storing the
  header unguarded would have converted this fix into a 500 on the login
  of whoever sent a long one.
- **It is a display label, never identity.** The value is unverified
  client input; nothing authenticates or authorizes on it. A request with
  no User-Agent (a bare API client) still stores `NULL`.

**What this does not do.** There is no IP address, no geolocation, and no
attempt to canonicalize a User-Agent into "Chrome on macOS" — the raw
header is what is stored and shown. Sessions created before this change
keep their `NULL`, and rotate with it, until the user logs in again.
`tests/api/test_auth_sessions.py::test_sessions_lists_device_info_and_revoke_removes_it`
had set a `User-Agent` header and named itself after `device_info` while
asserting only on the session count and id — blind-spot shape (e), a test
whose name claims a contract its assertions never reach. It now asserts
the value.

## Revoking a session did not revoke its access token (§69)

Every §69 remediation path — `POST /auth/logout`, `POST
/auth/sessions/{id}/revoke`, and the refresh-token-reuse containment in
`app/auth/service.py` — does exactly one thing: set `UserSession.revoked`.
Nothing that reads an access token ever looked at that column.

`create_access_token(user_id)` minted a JWT carrying only `sub`; the
session id went into the *refresh* token alone. So `get_current_user` —
the dependency in front of every authenticated REST endpoint — decoded the
token, loaded the user, and returned it. `_authenticate` in
`app/api/websockets.py` did the same for the live streams. Neither could
tell a live session from a revoked one, because neither was told which
session it was looking at.

Measured end to end, all three paths:

```
1) POST /auth/logout
   before logout : GET /auth/sessions -> 200  ([{...one session...}])
   logout        -> 204
   AFTER logout  : GET /auth/sessions -> 200  ([])

2) POST /auth/sessions/{id}/revoke
   revoke        -> 204
   AFTER revoke  : GET /auth/sessions -> 200  ([])

3) refresh-token reuse detected
   replaying the used refresh token -> 401
   rotated-to refresh token now     -> 401
   rotated-to ACCESS token          -> 200  ([])
```

The endpoint answered **"you have no active sessions"** with a `200`, to a
request authenticated by one of them.

Case 3 is the one that matters most, because `refresh()` states the
opposite in its own comment: it revokes every session for the user
*"so a thief's live access/refresh pair is cut off too"*. Only the refresh
half was cut off. After the system detected the textbook token-theft
signal and "contained" it, the thief kept full API access —
`access_token_expire_minutes` is 30 by default — which on this system is
enough time to place orders.

Access tokens now carry `sid`, exactly as refresh tokens always have, and
both readers resolve it through one shared `get_active_session` helper
that rejects a session that is revoked or past `expires_at`. The WebSocket
path needed it at least as much as the REST path: a stream opened just
before a revocation would otherwise keep pushing that user's live order
and position events for as long as the socket stayed up.

Three consequences worth stating rather than discovering later:

- **An access token minted before this change has no `sid` and is
  refused.** It names no session, so no revocation path can reach it;
  failing closed costs those holders one re-login, versus leaving an
  unrevokable credential in circulation.
- **Rotating a refresh token now retires the old access token
  immediately.** `refresh()` revokes the session it rotates out of, so the
  access token issued from it dies with it instead of lingering for its
  remaining lifetime. That is the point of rotation, but a client that
  refreshes while requests are still in flight on the old token will see
  those 401 — it must use the new token from the moment it rotates.
  `test_rotating_a_refresh_token_retires_the_old_access_token` pins it.
- **One extra `SELECT` per authenticated request.** `get_current_user`
  already did a user lookup per request; this adds a session lookup beside
  it. Stateless-JWT purity was never actually on offer here — the
  user lookup was already there — and revocation that cannot revoke is
  not a performance win.

**What this does not change.** Token lifetimes are untouched (30-minute
access, 30-day refresh). There is no admin "log this user out everywhere"
endpoint; the paths above are the only ones that revoke. And
`tests/api/test_auth_sessions.py::test_sessions_lists_device_info_and_revoke_removes_it`
had to be rewritten to revoke device A's session while listing from device
B — revoking your own session and then listing with that same token only
ever worked *because* of this hole, which is a fair illustration of how
quietly it sat.

## A live position's stop was a number nobody acted on (§57, §60)

`POST /orders` requires a `stop`. It sizes the position from it
(`calculate_position_size`), the risk engine gates the trade on it, and
since blueprint §60 was wired up it is attached to the resulting
`PositionRecord` and persisted to `positions.stop`. Then nothing happened.

Grepping every reader of a position's stop finds exactly one enforcement
point: `app/paper/engine.py`, which compares it against each candle fed
into `on_candle`. A live position has no candle loop. No worker polled
quotes against open stops; `ReconciliationWorker` compares local and
broker *positions*, not prices. So for a live order the stop was recorded,
displayed, and never enforced — price could run straight through it with
the position open and losing.

Measured before the fix, a long of 10 units entered at 100 with a stop at
95, after the market fell to 80:

```
positions = [('ACME', 10, 100.0)]     # still open
loss = -200.00                        # the stop would have capped it at -50.00
```

The fix is what a real desk does: rest the stop **at the broker**. A
broker-side stop keeps working while this process is restarting, wedged,
or disconnected — which is precisely when a local watcher loop would fail
and precisely when the stop matters most. `app/trading/protective_stops.py`
places one as soon as an entry fills, and `ensure_protective_stop` is
called after *every* fill on the position, because a fill can invalidate
the resting order in three different ways: adding to a position leaves the
old stop covering only part of it, a flip leaves it on the wrong side, and
a close leaves it resting against nothing — which at a real broker becomes
a **new naked position in the opposite direction** the moment it fires.

Two supporting pieces were required.

**`MockBroker` could not hold a stop at all.** `place_order` sent SL/SL_M
through `_resolve_fill_price`, which reads `request.price` for any
non-MARKET type. An SL_M is market-once-triggered and carries no limit
price, so it resolved to `None` and came back `REJECTED` —
`"No usable price for a SL_M order on ACME"`. A protective stop was
unplaceable through the only broker any test or paper account actually
uses, which is also why no test could ever have covered this. It now holds
resting stops and fires them when `set_quote` crosses the trigger: a
sell-stop at or below, a buy-stop at or above. `UpstoxBroker` already
mapped `SL`/`SL_M` and sent `trigger_price`, so the live adapter needed no
change — that half was ready and had nothing calling it.

**`positions.protective_order_id` is a new column**, not in-memory state.
The order it names lives at the broker and outlives this process; without
a durable handle, an API restart would leave a real protective order
resting with nothing able to cancel or replace it, and the next entry on
the same symbol would stack a second one beside it.

Some deliberate choices worth stating:

- **SL_M, not SL.** Once a stop triggers, getting out matters more than
  the price. A stop-limit can go unfilled in exactly the fast market that
  triggered it, leaving the position open with its protection spent.
- **A gap fills at the gapped price, not the trigger.** The test pins
  this: a stop is a trigger, not a guaranteed price, and reporting the
  trigger would overstate every result by the size of the gap.
- **Replace rather than modify.** `Broker.modify_order` is optional
  surface not every adapter implements meaningfully; cancel and place are
  the two calls every adapter must support.
- **An inverted bracket now closes immediately.** A long whose stop sits
  *above* its entry produces a sell-stop already through its trigger, and
  a real broker fires that at once. Nothing validates stop direction
  against entry direction, so this is the visible consequence of an order
  that was always wrong — surfaced now instead of resting behind
  protection that could never help it.

**What this does not do.** It does not add a local watchdog for brokers
that reject stop orders outright: `ensure_protective_stop` logs and
returns `None`, and the position is then unprotected exactly as before —
a follow-up should halt the account in that case. Target/take-profit
orders are not placed; only the stop is. The paper and auto-trade engines
are untouched, since they already enforce stops against the candles they
are fed, and giving them broker-side stops as well would close positions
twice. And this has never run against a real broker — `UpstoxBroker`'s
stop payload is unverified against Upstox's live servers.

## A timeout placing an order was neither caught nor distinguishable (§50, §75)

`Broker.place_order`'s contract is explicit that a failure must come back
as an `OrderResult`, never as an exception, and says why: `ExecutionEngine.
submit` has no try/except around the call, and by the time it runs the
order is already registered under its idempotency key. An exception both
500s the request *and* wedges the order permanently — a retry with the
same parameters returns `created=False` and never calls `submit()` again.

`UpstoxBroker.place_order` honoured that for an HTTP 4xx/5xx
(`HTTPStatusError`) and for Upstox's 200-with-error-envelope
(`BrokerError`). It did not catch a transport failure. A connect timeout,
a read timeout, a dropped connection, or a proxy answering with something
that is not JSON all escaped into the caller — the single most likely
failure mode in a real deployment, since it needs nothing more than a
flaky network.

The suite could not have caught this: **no broker double anywhere in it
raised from `place_order`**. Every fake returned a well-formed
`OrderResult`.

The subtle half is what the result should say. A timeout is **not** a
rejection:

- `REJECTED` asserts that no order reached the market and the account is
  flat. Every downstream consumer reads it that way — the position
  manager applies no fill, the risk engine's exposure math assumes
  nothing opened, and `repeated_rejections` counts it as a broker saying
  no.
- A timeout may well have placed a real order that is filling right now.

So a transport failure returns `OrderStatus.FAILED` with a reason that
says the order's fate is unknown, and `ExecutionEngine.submit` grew a
branch for it. Without that branch the order fell through to
`transition(ACKNOWLEDGED, "broker acknowledged")` — recording that the
broker confirmed something it never said. `FAILED` is exactly the state
`ReconciliationWorker` exists to resolve against the broker's own record,
and a broker-side position with no local match already halts the account
(§75) rather than trading on top of an unknown.

`SUBMITTED -> FAILED` was already an allowed transition; nothing had ever
used it.

**What this does not do.** It does not retry, and it must not: retrying an
order whose fate is unknown is how you end up with two positions. It does
not reconcile eagerly — the order sits FAILED until the next
reconciliation pass. `DhanBroker` is still a skeleton and is not covered.
And this is tested against `httpx.MockTransport`, not against Upstox's
real servers, so it proves the adapter's behaviour on a transport failure,
not that Upstox fails in exactly these ways.

## The blueprint's own flagship strategy could not fire (§34, §17)

Blueprint §34 prints one worked strategy — "Bullish Liquidity Sweep":
sell-side liquidity swept, market structure shifts bullish, a bullish FVG
forms, enter on the retest. It is the platform's thesis in five lines of
JSON. It appeared nowhere in the codebase, was never executed by anything,
and when finally run, it did not work.

Written exactly as the blueprint prints it and evaluated over 120 seeded
random walks of 160 bars — roughly 9,600 bar-evaluations — it matched
**twice**. Not rarely. Effectively never.

The cause is a single default. `Condition.lookback` was `5` for every
condition type, and both the sweep and the MSS check
`current_index - event_index < lookback`. But those two events cannot be
five bars apart. An MSS is a *sequence*: a CHoCH breaking a confirmed
swing, then a same-direction BOS breaking a later confirmed swing, each
waiting `swing_length` bars for its swing to confirm
(`app/smc/structure.py`, `app/smc/swings.py`). By the time the structure
shift exists, the sweep that caused it has aged out of its own window. The
strategy asks for a sequence and the DSL only ever looked at an instant.

Measured across 3,120 bar-evaluations, varying only the window:

```
lookback   sweep+mss   all three
       5           1           1
      10           9           7
      20          62          45
      40         376         288
      80         996         804
```

`_DEFAULT_LOOKBACK_BY_TYPE` in `app/strategy/dsl.py` now defaults the
window from the event's own formation time: 30 bars for the multi-bar
structure events (MSS, CHoCH, BOS, liquidity sweep), 20 for order blocks,
5 for single-print events like an FVG. The same §34 JSON now matches 374
times over the original corpus, up from 2. An explicit `lookback` is still
always honoured, including one shorter than the default — the table is a
default, not a floor.

### A strategy library that is proven to fire

`app/strategy/library.py` ships five strategies —  the §34 example, its
bearish mirror, bullish and bearish order-block retests, and a
discount-zone FVG long — exposed through `GET /strategies/library` and
copied into a user's own strategies by `POST /strategies/library/{key}`.
A copy, not a reference: later edits to the shipped library never silently
alter a strategy someone is trading.

The point of the accompanying test is not that these are good strategies.
It is that each one **matches at least once** on a varied corpus, with an
entry and stop that actually resolve. Before this, nothing in the
repository demonstrated that any complete strategy produced a signal end
to end, which is exactly how the flagship example stayed broken. Observed
hit rates over 2,080 bar-evaluations:

```
bullish_liquidity_sweep         4.7%
bearish_liquidity_sweep         4.5%
bullish_order_block_retest     34.2%
bearish_order_block_retest     27.7%
discount_fvg_long              25.1%
```

The spread is the design: the sweep strategies demand a reversal sequence
and are selective; the retests take no view on exhaustion and fire far
more often.

**What this does not establish.** The corpus is seeded random walks, not
market data. It is a lower bound on *expressibility* — price action varied
enough that a setup which can occur, does — and says nothing whatever
about profitability. No library strategy has been validated on real data;
`POST /backtest/validate` exists for exactly that and remains unrun against
a real feed. The hit rates above are a property of the corpus, not an edge.
Two of the five strategies fire on a quarter to a third of all bars, which
is a frequency to be suspicious of, not proud of.

## A half-executed spread is a naked option

A defined-risk options combination is only defined-risk while every leg
exists. `app/risk/options_risk.py` gates a strategy on the *combined*
payoff `compute_payoff_summary` produces — a 25200/25000 bull put spread
is approved because the long put caps the short one at a 6,500 loss. The
exchange enforces none of that pairing: each leg is a separate order, and
the broker is free to fill one and reject the next.

Measured, on a 100,000 account: the short put filled, the protective long
put came back rejected, and the account was left holding a naked short
put whose real maximum loss is the strike (1,254,000 — roughly 193× what
was approved, and 12× the whole account). The response was `201 Created`
with a "position opened" notification, no halt, and no indication
anywhere that the approved combination did not exist. Each leg's status
was reported accurately; nothing drew the conclusion from them.

`_remediate_partial_batch` (`app/api/options.py`) now draws it. A leg
counts as established only when it is completely filled; if any leg is
not, and anything at all executed, the batch is broken and:

- **A leg the broker never answered (`FAILED`) is never unwound.** Since
  `UpstoxBroker.place_order` gained its transport-failure branch, that
  status means *fate unknown*, not *rejected* — an opposing order against
  a position that may or may not exist is a new position half the time.
  Those go to reconciliation, and the filled legs are deliberately left
  alone.
- **Otherwise the exposure this call newly opened is given back** with
  opposing market orders, journalled through the same `_submit_leg` path
  as the original legs so the unwind is a real, audited order and not an
  invisible adjustment. The quantity is what the *position gained*, not
  what the order filled: a fill that closes 50 long and opens 50 short
  opened 50, and unwinding 100 would leave the account long again.
- **The account is halted either way** (`app.core.redis.halt_account`,
  blueprint §73-75), because an unwind is best effort — it can be
  rejected in turn, and it does not restore a leg the batch merely
  *reduced*. The halt exempts reducing orders, so the holder can still
  get out; what it stops is piling more on top.

The one case that does not halt is a batch where no leg reached the
exchange at all: nothing was opened, so there is nothing to remediate and
locking the account would be punishment for an order the exchange never
saw.

The response carries `strategy_intact` and `remediation` so a client
reading `max_loss` can tell whether that number describes a position that
exists. It says nothing about *whether the strategy was a good idea* —
only whether the thing the risk engine approved is the thing the account
is holding.

## A protective stop the broker refuses (§57, §60, §73-75)

Resting the stop at the broker (above) only helps if the broker takes the
order. `ensure_protective_stop` returned `str | None` and its single
caller, in `POST /orders`, discarded the value entirely — so a broker that
refused the stop produced an `ERROR` in a log file and nothing else:

```
HTTP 201  status MONITORING
position qty=100.0  stop=95.0  protective_order_id=None
resting stop orders at the broker: 0
Account halted? None
notifications: ['TRADE_EXECUTED']
```

The order genuinely filled. The position genuinely exists. It carries the
stop price it was *sized from* — `calculate_position_size` divides the
risk budget by the entry-to-stop distance, so the approved risk is
explicitly conditional on that stop — and nothing at the broker will ever
act on it. The only thing the account holder was told is "position
opened". Brokers refuse stop orders routinely: a trigger too close to the
last traded price, a freeze quantity, stop orders not accepted for a
segment at that moment.

The `str | None` return is what hid it. `None` spelled two different
things — "no stop was wanted here" (the position closed, or carries no
stop price) and "a stop was wanted and the broker refused it" — so there
was nothing for a caller to check even if it had looked.
`ProtectiveStopResult` now separates them, and keeps a third case apart
too: a stop order that came back `FAILED` means the broker never
answered, so a stop may or may not be resting. That is not the same as
knowing there isn't one, and `fate_unknown` carries the difference into
the notification and the audit row — a reconciliation that assumed "bare"
could place a second stop on top of a live one.

When a fill leaves a position wanting a stop it does not verifiably have,
`POST /orders` now halts the account (`halt_account`), writes an
`order.protective_stop_unplaced` audit row, fires a
`RECONCILIATION_REQUIRED` notification, and returns the reason in the
response as `unprotected_reason`.

**The position is deliberately not closed automatically.** That is a
judgement call, and the opposite one from the half-executed options spread
above, so it is worth being explicit about why. There, the combination the
risk engine approved never existed and the naked leg's real risk was ~193×
what was approved; unwinding restored the state the approval was
conditioned on. Here the position is exactly the one the caller asked for
— only its protection is missing, often for a transient reason — and
liquidating it at market on a broker quirk is a decision that belongs to
whoever owns the account. The halt exempts reducing orders, so closing it
by hand stays available while nothing new can be opened on top.

What this does **not** do: retry the stop, place a fallback order type, or
watch the position afterwards. An account in this state needs a human,
which is what the halt and the notification are for.

## No market data is not fresh market data (§57)

`RiskCheck("market_data_fresh")` exists so an order is never sized or
priced against information the platform cannot vouch for.
`get_price_age_seconds` returns `None` when no price has ever been seen
for a symbol, or its TTL has expired — its own docstring calls that
"there is nothing fresh to trust". `POST /orders` read it as
`... or 0.0`, the **freshest possible value**.

Measured against a connected (non-`MockBroker`) stack, two orders on two
instruments:

```
CASE A -- no market data at all
  get_price_age_seconds -> None
  HTTP 201 -> MONITORING

CASE B -- market data exists but is 60s stale
  get_price_age_seconds -> 60.0
  HTTP 403 -> Data age 60.02s vs max 10.0s
```

A price a minute old was refused; no price whatsoever was filled, and
journalled `LIVE`. Worse information passed the gate that better
information failed.

The lenient default was not an accident — a comment introduced it
deliberately, and its reasoning was right about the case it named:

> nothing to be stale relative to, so treat that case as fresh rather
> than blocking every order **in a system with no broker connected**

That is the `MockBroker` case, Stage 9's honest default, where no
market-data worker is expected to be running and blocking every paper
order would be nonsense rather than safety. The error was applying that
conclusion to a system which *does* have a broker connected. There, "we
have no idea what this instrument is trading at" is precisely when not to
send an order, and a market-data worker that has died is the exact
failure this check exists to catch.

So `_market_data_age_for` narrows the leniency to the case that was
actually argued for: `None` from Redis becomes `0.0` for a stack trading
against `MockBroker`, and stays `None` for a real one.
`TradeRiskProposal.market_data_age_seconds` is now `float | None`, and the
engine treats `None` as a distinct failure — *"No market data for this
instrument"* — rather than a number to compare. A caller that cannot say
how old the data is has not said it is fresh.

Two things deliberately left alone:

- **`recent_price_jump_pct`'s `or 0.0` on the next line is correct.** No
  prior tick genuinely is no jump, not an unknown one. The two lines look
  identical and mean opposite things, which is most of why this survived.
- **`OptionsRiskProposal.market_data_age_seconds` keeps its `0.0`
  default.** No options-chain ingestion exists at all (see above), so
  every leg would block rather than degrade — a different situation with
  its own documented reasoning.

## The engine got slower every bar it ran (§23, §54)

`SMCEngine.analyze` was quadratic in history length, and the autonomous
loop calls it on the whole history every pass.

`update_mitigation`, in both `app/smc/fvg.py` and
`app/smc/order_blocks.py`, recomputed each zone's fill state by scanning
*every* later candle — and the number of zones grows with the series. So
the work per call grew as zones × candles. `PaperTradingEngine.on_candle`
calls `analyze` over its entire accumulated history once per bar,
`AutoTradeSupervisor` drives that on a 60-second loop, and
`auto_trade_worker.py` seeds `engine.candles` once from an unbounded
`get_candles` and appends to it forever after.

Measured, one `analyze` pass over a random walk:

```
   bars   before    after
    500   11.2ms    5.4ms
   2000  136.6ms   30.0ms
   8000  1988ms     214ms
  16000  7560ms     657ms     (11.5x)
```

16 000 bars is about eleven days of one-minute data for **one** instrument
on **one** strategy. At 7.5 seconds per pair per pass, a handful of pairs
already exceeded the 60-second interval — and the overrun grew with every
bar stored, so the loop fell progressively further behind rather than
settling at a steady lag.

`cProfile` put 4.1s of an 8 000-bar pass in `fvg.update_mitigation` and
1.5s in the order-block twin, with 9 million `max()` and 8.8 million
`min()` calls. Note what this is *not*: the candle list itself is 1.4 MB
at 16 000 bars. This was never a memory problem, and the earlier
"unbounded growth" framing of it was wrong — the list is small, the
re-analysis is what costs.

The fix is to stop each scan once the outcome can no longer change. Both
fields anyone reads are monotone: `filled_percentage` is
`min(deepest_fill / size, 1.0)`, so it is pinned once `deepest_fill`
reaches `size`, and `mitigated` is sticky-true from that same point. Order
blocks already computed `mitigated_index`, so their exact stopping
condition was sitting there unused.

One divergence, stated rather than buried: `FairValueGap.invalidated` can
now be left `False` where a full scan would eventually have set it — a gap
filled gradually and engulfed only much later. Nothing outside `fvg.py`
reads it (`SMCEngine.active_fvgs`, the chart overlay and the AI context
all read `mitigated`/`filled_percentage`), and an engulfing candle fills a
gap completely in the same iteration it invalidates it, so invalidation
never arrives before the fill that ends the scan anyway.

A faster analysis that produced different zones would be worse than a slow
one, so `tests/smc/test_mitigation_equivalence.py` carries the
pre-optimisation full-scan loop as a reference implementation and asserts
the real one agrees on every consumed field across 25 randomised walks.
A stash-verify proves nothing for a change like this — the old code is
also correct — so the test's own credibility is established by injecting a
plausible-but-wrong early exit (break on first overlap), which it catches.

## Two more scans that should have been lookups (§22, §24)

Stopping the settled-zone rescans (above) made `SMCEngine.analyze` about
11x faster but left its *shape* unchanged — still quadratic, just with a
smaller constant. Measured after that change:

```
    bars   days of 1m   analyze
   16000           11     0.61s
   32000           22     1.92s
   64000           44     5.81s
  128000           89    22.83s
```

So the wall moved from roughly eleven days of stored history to roughly
three months, and then reappeared. `cProfile` named the two sites holding
the shape up, both asking a bounded question by way of an unbounded scan:

- **`detect_order_blocks`** asked, for every structure event, "is there a
  same-direction FVG within ±2 bars of this break?" — and answered it by
  scanning every gap. Both lists grow with the series: 2.4 million
  generator steps over 16 000 bars. The question only ever uses
  `created_index`, so indexing the gaps by it once turns each answer into
  five dict lookups.
- **`detect_equal_levels`** grouped swings into equal-highs/equal-lows
  pools by scanning every candidate for each anchor — O(swings²). But
  `candidates` is *already sorted by price* and the grouping test is a
  band around the anchor, so every group is a contiguous run: `bisect`
  bounds the scan to the band.

```
    bars   before    after
   16000    0.61s    0.36s
   32000    1.92s    1.04s
   64000    5.81s    2.34s
  128000   22.83s    5.82s     (3.9x)
```

The bisect window is deliberately widened by one position on each side and
the original predicate — `abs(s.price - anchor.price) <= tolerance` — still
decides membership. The band bounds are recomputed floats, so a swing
sitting exactly on the boundary could otherwise fall a single ulp outside
the slice; the slice is an optimisation of *where to look*, never of *what
counts*.

### A test that was checking a copy of the logic

Worth recording, because the mistake is easy to repeat. The first version
of the order-block test rebuilt the `created_index` index inside the test
and compared that to the reference scan. It passed — and kept passing when
an off-by-one was injected into the real ±2 window, because it had never
touched the shipped code at all. A test that duplicates the implementation
cannot fail when the implementation changes.

The replacement recovers the predicate's answer *through* `detect_order_blocks`:
`fvg_score` contributes exactly 0.3 to `strength`, so differencing a normal
run against one given no gaps recovers whether the lookup found an adjacent
gap. That version does catch the injected off-by-one, as does the
equal-levels test when the bisect window is deliberately mis-set.

## `?limit=-1` was a 500 (§116)

`limit` was declared `int` on every listing endpoint, with an upper cap on
some and none at all on others — but with **no lower bound anywhere**. A
negative value went straight into `.limit(...)`, which Postgres rejects:

```
GET /notifications?limit=-1     -> HTTP 500  (asyncpg InvalidRowCountInLimitClauseError)
GET /ai/chat/history?limit=-1   -> HTTP 500  (same)
GET /ai/chat/history?limit=1000000000 -> HTTP 200
```

A server error for what is purely a bad request. This is the same shape as
the AI-proposal validator (§ above): input whose shape the code does not
admit, reaching a layer that raises on it. Two endpoints — `GET
/ai/chat/history` and `GET /setups` — additionally had no upper bound, so
one request could ask for an entire table.

All eight numeric query parameters now carry `ge=1`, and the two uncapped
ones carry `le=500`. FastAPI rejects out-of-range values with a `422`
naming the parameter, before any query is built.

Two things worth stating rather than leaving implicit:

- **`ge=1` also changes `?limit=0`** from `200` with `[]` to `422`. `ge=0`
  would have fixed the 500 on its own. "Return me at most zero rows" is a
  client error rather than a request worth serving, so the stricter bound
  is deliberate — but it is a behaviour change, not a pure bug fix.
- **The traceback in that 500 body is not a code defect.**
  `app/core/config.py` has `debug: bool = True` by default and
  `main.py`'s handler returns `str(exc)` when debug is on, so a deployment
  that leaves `DEBUG` true serves tracebacks and echoes every SQL
  statement. That is a deployment-configuration concern, separate from
  this fix and not addressed by it.

## The production guard did not cover `debug`

`Settings._refuse_unsafe_defaults_in_production` makes `ENVIRONMENT=production`
a hard startup failure when `JWT_SECRET` or `CREDENTIALS_ENCRYPTION_KEY` is
still a repo default. It did not check `debug`, which defaults to `True`:

```
ENVIRONMENT=production started fine with debug=True
```

Two readers already in the tree make that unsafe:

```
app/main.py:107             detail = str(exc) if settings.debug else "Internal server error"
app/database/session.py:32  create_async_engine(..., echo=settings.debug, ...)
```

So a deployment that correctly overrode both secrets but never set
`DEBUG=false` answers every 500 with raw exception text — including the
failing SQL statement — to unauthenticated clients, and logs every
statement it runs. The `?limit=-1` bug above is a worked example of what
that leaks: the asyncpg error *plus* `[SQL: SELECT notifications.id, ...]`.

This is the same failure mode the guard already existed to catch — an
operator not overriding a development default — so it is now refused the
same way, and the validator is renamed from
`_refuse_default_secrets_in_production` to reflect that it guards more
than secrets.

**Refused rather than silently forced to `False`.** The alternative was to
override the value and log a warning. An operator who set `DEBUG=true`
deliberately should find out at startup rather than discover later that
the setting was ignored — and a refusal cannot be missed in a log. It is
stricter than it needs to be for safety alone, which is the point.

Two supporting changes, without which the guard would be a trap rather
than a check: `.env.example` had **no `DEBUG` line at all**, so an
operator following `PRODUCTION_READINESS.md` would set
`ENVIRONMENT=production`, inherit `debug=True` invisibly, and meet a
startup failure with nothing in their config to point at. It now ships a
documented `DEBUG=` line, and the production checklist gained the matching
bullet.

## A price is untrusted input too (§57, §60)

Bounding the query parameters (above) left request bodies unaudited, and
`PlaceOrderRequest.entry`/`stop`/`price` were plain unbounded floats that
flow all the way to `Numeric(18, 6)` columns — which hold at most
999999999999.999999.

`entry=1e308` was risk-approved (`calculate_position_size` divides the
risk budget by the entry-to-stop distance, so a huge distance sizes a
*tiny* quantity, and every notional check then looked small), **filled at
the broker**, and its order row committed. Then:

```
asyncpg.exceptions.NumericValueOutOfRangeError: numeric field overflow
  app/api/orders.py:609 in place_order
  app/trading/persistence.py:214 in persist_position

in-memory orders left behind: 1  (status=MONITORING, qty=5e-306)
persisted order rows: 1
```

One request, from any authenticated client, produced a three-way
divergence: a position at the broker, an order journalled `MONITORING`,
live `PositionManager` state, and **no row in `positions`** — plus a 500.
Negative and zero prices were reaching the broker too, coming back as
rejections rather than being refused at the boundary.

`entry`/`stop`/`price` now carry `gt=0, lt=1e12`. `gt=0` also rejects
non-finite values without a separate rule: NaN fails every comparison and
`inf` fails `lt=`.

### The fix uncovered a second, general bug

Adding those bounds turned `entry=1e400` from a 403 into a **500**, which
is worse — so it was worth chasing rather than accepting. The cause is not
in this endpoint at all:

```
ValueError: Out of range float values are not JSON compliant
```

A validation error echoes the input that failed, JSON has no literal for
infinity, and FastAPI's default `RequestValidationError` handler
serialises that payload — so the 422 could not be written and the
unhandled-exception handler turned it into a 500. **Every** endpoint with
a bounded numeric body field had this, which was confirmed on
`POST /auto-trading/enable` (whose `risk_per_trade_pct` bound long
predates this change) against otherwise-unmodified code:

```
risk_per_trade_pct=1e400  -> HTTP 500
risk_per_trade_pct=200    -> HTTP 422
```

So `app/main.py` now installs a `RequestValidationError` handler that runs
`jsonable_encoder` first — exactly as FastAPI's own default does, because
a `model_validator` failure carries a raw `ValueError` in its `ctx` — and
then replaces non-finite floats with their `repr` so the offending value
is still reported, just printably. Fixed once, centrally, rather than
per-field, so no future bounded field has to remember.

## An options leg had no price or size bounds at all (round 102)

`POST /orders` got its price bounds in the previous round. `POST
/options/execute` — the *other* endpoint that reaches a broker and writes
to `orders`, `trades` and `positions` — did not, and its request schema
carried no `Field` constraints whatsoever:

```python
class ExecuteOptionLegRequest(BaseModel):
    symbol: str
    direction: Direction
    quantity: float   # unbounded
    premium: float    # unbounded
```

Probing a two-leg spread with a pathological value in one leg, against
unmodified code, gave:

| leg value | result |
|---|---|
| `premium=-100` | **HTTP 201, executed**, `max_profit: 17500` |
| `premium=1e308` | 403, "Projected exposure inf% vs limit 100.0%" |
| `premium=1e400` | 403, same |
| `quantity=-5` | 403, "Projected exposure 3797.50% vs limit 100.0%" |
| `quantity=0` | 403, "Projected exposure 627.50% vs limit 100.0%" |

Only the first row is a genuine defect, and the other four deserve to be
described accurately rather than claimed as working validation:

- The `inf` rows are refused because **every** comparison against `inf`
  is False, so `projected_exposure <= limit` fails. That is the risk
  engine failing closed on a value it was never given a name for, not the
  engine recognising a bad premium.
- The negative- and zero-quantity rows are refused by an exposure
  percentage computed largely from the *other*, well-formed leg. Change
  the sibling leg and the same malformed input can pass.

The real failure is the first one. A negative premium is not a price; it
is a credit dressed as a debit. The strategy priced and executed with a
`max_profit` of 17500 on a spread whose true payoff is nothing of the
sort, and both legs reached the broker.

The fix mirrors `PlaceOrderRequest` exactly, for the same reason — every
price and quantity column in `app/database/models/trading.py` is
`Numeric(18, 6)`, which holds at most `999999999999.999999`:

```python
_MAX_PREMIUM = 1e12

class ExecuteOptionLegRequest(BaseModel):
    symbol: str
    direction: Direction
    quantity: float = Field(gt=0, lt=_MAX_PREMIUM)  # number of lots
    premium: float = Field(gt=0, lt=_MAX_PREMIUM)
```

All five probes now return 422 naming the offending field, before any
risk evaluation, broker call or database write. The central
`RequestValidationError` handler added in round 101 is what lets the
`1e400` case return a 422 rather than a 500.

This still leaves unbounded numeric body fields elsewhere —
`BuildStrategyRequest.quantity` / `lot_size`, replay
`CreateReplayRequest.starting_balance` / `swing_length`, and backtest
`starting_capital`. None of those reach a broker, which is why the two
execution endpoints came first; they remain open.

## An analysis knob is a cost knob too (round 103)

The bounds added in the previous rounds all landed on a parameter called
`limit`, or on a price. `swing_length` is the same class of mistake one
name over: a client-supplied integer handed to an engine that has its own
opinion about what is valid, with nothing in between.

`GET /charts/{instrument_id}/smc` declared it a bare `int`:

```python
swing_length: int = 3,
...
context = SMCEngine(SMCConfig(swing_length=swing_length)).analyze(candles)
```

and `app/smc/swings.py` opens with

```python
if swing_length < 1:
    raise ValueError("swing_length must be >= 1")
```

That `ValueError` had nothing to catch it, so it reached the catch-all
handler. Probed against unmodified code on an instrument with 200 candles:

| `?swing_length=` | result |
|---|---|
| `3` (default) | 200 |
| `0` | **500** |
| `-1` | **500** |
| `-100` | **500** |
| `95` | 200 |
| `10^9` | 200 |

A 500 for what is purely a malformed request, on a GET any authenticated
user can issue.

The upper bound added alongside it is a **cost** bound, not a correctness
one, and worth stating separately because nothing about it looks wrong
from the outside. `detect_swings` builds and scans a `2 * swing_length + 1`
window per bar, so the parameter multiplies the work per request. Measured
directly over 16000 candles:

| `swing_length` | `detect_swings` |
|---|---|
| 3 (default) | 26 ms |
| 100 | 250 ms |
| 1000 | 1818 ms |
| 4000 | 3976 ms |

A value large enough to empty `range(swing_length, n - swing_length)`
returns 200 immediately — which is why `10^9` looked harmless in the table
above. The expensive region is the one just below that, and it is reached
with a single integer in a query string on the event loop every other
request shares. A pivot with more than 100 bars either side is not a
structural swing anyone reads, so the parameter is now
`Query(default=3, ge=1, le=100)`.

`POST /replay` takes the same two knobs in its body, and they are bounded
here too — but honestly labelled: that one did **not** 500. `SMCConfig` is
built there and stored on the engine, yet `ReplayEngine.analyze` has no
caller anywhere in `app/`, so the value is never used. The bound closes a
trap before it is stepped in rather than fixing a live crash, and the
request model says so at the field. `starting_balance` gained `gt=0` on
the same pass; a replay session starting from zero or negative capital is
not a scenario, and every statistic `compute_statistics` returns is a
ratio against it.

One sibling was checked and deliberately left alone: `POST
/replay/{id}/step?steps=` is also an unbounded `int`, and it is genuinely
harmless. `ReplayEngine.advance` iterates `range(steps)` — negative is an
empty range, and the loop breaks on `clock.is_finished`, so a huge value
stops at the end of the series rather than spinning. Bounding it would be
tidiness, not a fix.

## A second opinion on the SMC detectors (round 104)

`app/smc` is hand-written, and every correctness claim about it so far has
been checked against tests we also wrote. `app/smc/reference.py` adds an
independent implementation to check it against: the `smartmoneyconcepts`
package (MIT), wrapped so its detectors can be run over the same
`list[Candle]` and the two compared.

It is a **development dependency** (`requirements-dev.txt`, which CI now
installs) and is imported lazily inside each adapter function, so neither
it nor its numba/llvmlite dependency (~64MB) enters the production image
or the application's import graph.

### Why it is barred from the live path

The obvious thing to do with a maintained library is to adopt it outright.
That was measured before it was decided, and the measurement says no.
Blueprint §45 makes look-ahead prevention mandatory. Feeding each engine
one more candle at a time, over 249 arrivals:

| | verdict revised on an already-visible bar | swings retracted |
|---|---|---|
| `smartmoneyconcepts` | 37 (FVG) | **253** |
| `app.smc` | 0 | **0** |

`smc.fvg` decides row `i` with `.shift(-1)` — candle `i + 1`.
`smc.swing_highs_lows` post-filters into a strictly alternating HIGH/LOW
sequence, recomputed globally on each call, so a swing it has already
reported can stop existing. A retraction means a level handed to a live
consumer later ceased to have happened; that is acceptable for offline
analysis and disqualifying for anything that places an order.

Our own detectors are monotone: over the same arrivals `app.smc` added 49
swings and retracted none, and every addition landed on the single bar
that had just become confirmable — exactly the relationship
`Swing.confirmed_index` encodes.

### What the two engines agree on, and what they do not

The engines differ by design, and the tests encode which differences are
understood rather than papering over them:

* **FVG** — the library additionally requires the middle candle to close
  in the gap's direction; we apply the plain three-candle imbalance. So it
  finds strictly fewer gaps (roughly half), and **every gap it finds is
  one we find, with the same direction**, on all ten test series. That
  subset relation is the assertion: if our detector ever stops seeing a
  gap an independent implementation still sees, the test fails.
* **Swings** — the alternation filter discards intermediate pivots, so the
  sets differ. Where both engines report a swing on the same bar, the
  HIGH/LOW classification has never conflicted.
* **Index convention** — the library anchors a gap at the middle candle;
  `FairValueGap.created_index` anchors at the third, the first bar on
  which the gap is knowable. The adapter normalises to ours, which is why
  `reference_fvgs` adds one.
* **Boundary artefact** — the library emits a swing at index 0 on every
  series tested, although bar 0 has no left window. The adapter drops it.

### An outside bar is both a swing high and a swing low

Writing the comparison surfaced this, and it is worth recording because
the first version of the test got it wrong. A bar whose high is the unique
maximum of its window *and* whose low is the unique minimum — an outside
bar that engulfs its neighbours — satisfies both pivot tests, and
`detect_swings` correctly emits two swings for it. Keying swings by bar
index collapses the pair and silently keeps one, which manufactures a
disagreement with the library that is not real. The test compares
`(index, type)` pairs for that reason. Seed 3 bar 28 of the fixture is
such a bar.

### The look-ahead test stands on its own

The third test in `tests/smc/test_reference_agreement.py` does not use the
library at all in its assertions. It pins the §45 guarantee directly —
feed one candle at a time, nothing already reported may be withdrawn, and
a new swing must land on the bar that has just become confirmable. That
guarantee was described as mandatory in the blueprint and had no test
before this. The library's numbers above are quoted in its docstring only
as the contrasting case.

## Replay could not show you the structure it was replaying (round 105)

Blueprint §41 states the replay flow explicitly:

```text
Replay Clock -> Current Timestamp -> Only historical information
available so far -> SMC/ICT -> Strategy -> AI -> Virtual Execution
```

`ReplayEngine.analyze` implements the SMC/ICT stage, correctly and
look-ahead-safely, and had **zero callers anywhere in `app/`**. No
endpoint, no worker; `/ws/replay` only relays `_state_response`, which
carries cursor, balance and open trade and no structure at all. A user
could step through a replay bar by bar and never see the swings, gaps,
order blocks or structure breaks forming — which is the entire point of
stepping.

This is a missing endpoint completing a specified feature, not a crash
being fixed, and it is described that way rather than dressed up as a bug.

`GET /replay/{session_id}/analysis` is that endpoint. It goes through
`_get_owned_session` like every other `/replay/*` route, so an unowned
session answers 404 rather than confirming it exists, and it reuses
`_serialize_smc` / `_serialize_ict` from `app/api/charts.py` so the replay
and chart views of the same structure cannot drift apart.

### The absence had a second cost

`ReplayClock.visible_candles` — the property whose own docstring says "the
single rule that matters here", and which is what blueprint §45 calls
mandatory — had exactly one consumer: `ReplayEngine.analyze`. With nothing
calling that, the guarantee was protecting nothing reachable, and no test
exercised it end to end. Substituting `self.clock.candles` for `visible`
would have left the whole suite green.

`tests/api/test_replay_analysis.py` now asserts it through the API: step
the cursor, fetch the analysis, and require that nothing in the payload is
dated later than the candle the cursor sits on. With that substitution
injected, the test reports the exact leak — at cursor 12 the analysis
carried structure dated from ten later candles.

Note this is a *different* failure from the one
`tests/smc/test_reference_agreement.py` pins. That test proves the
detectors never revise a past verdict. This one proves the layer above
hands them the truncated slice. A perfectly look-ahead-safe detector fed
the entire history leaks the future just as thoroughly, and only this
failure is reachable by a user stepping through a replay.

### A vacuous assertion, caught before it shipped

The first version of this test also asserted that swept liquidity pools
never decrease. It passed — and it was worthless: the fixture produced
**zero** swept pools at every cursor, so the assertion never evaluated
anything. `liquidity_pools` is the one section the date bound cannot
check, because the chart serialiser emits side, source, price, swept and
rejected for a pool and no timestamp at all, and `swept` is exactly the
field a full-series leak inflates, since `detect_sweeps` scans the candles
*after* a pool forms.

The fixture now carries a tail that takes out the 130 equal-highs level
partway through, so a sweep genuinely occurs during the replay: the
sequence measured across cursors 12, 18, 24, 29, 33, 39 is
`0, 0, 0, 0, 1, 1`. The test requires it to start at zero, end above zero,
and never decrease — which a full-series leak fails on the first of those,
because it would report the sweep from the very first request.

## A zero close desynchronised the correlation pair (round 106)

`build_correlation_matrix` intersects each symbol pair on the timestamps
they genuinely share before computing returns, and its docstring explains
why at length: correlating by list position "silently compares one
instrument's recent history against another's older history and reports a
number with no meaning". That care was undone one step later.

The returns themselves came from `_returns`, which drops a return whose
previous close is zero:

```python
[(closes[i] - closes[i - 1]) / closes[i - 1]
 for i in range(1, len(closes)) if closes[i - 1] != 0]
```

That is correct for one series and wrong for two. A zero close in one
instrument and not the other leaves the two lists **different lengths**,
and `pearson_correlation` then falls back to truncating from the tail —
pairing one instrument's bars against the other's *neighbouring* bars.
The positional misalignment the timestamp intersection exists to prevent,
reintroduced immediately after it.

Measured on two symbols following the **identical** path at the
**identical** timestamps, differing only by a single zero close near the
end of a 60-bar window:

| | reported correlation |
|---|---|
| before | **−0.5224** |
| after | **+0.5224** |

Same magnitude, inverted sign — the signature of a one-bar shift on an
alternating series. Two instruments that move together were reported as
moving against each other.

The fix is `_paired_returns`, which walks the shared timestamps once and
emits a return for both instruments or neither, so the two series are
equal-length by construction rather than by luck:

```python
if previous_a == 0 or previous_b == 0:
    continue
```

A zero close is not hypothetical. Nothing in `upsert_candles` validates
one, and an options contract that expires worthless prints exactly that.

**What is deliberately not claimed.** At the default
`correlation_threshold` of 0.7 neither value trips
`correlated_exposure_limit`, so this is not a demonstrated change of risk
verdict, and the test says so in as many words. What it is, is a
meaningless number produced by a module whose entire purpose is a
meaningful one — the same standard its own docstring sets.

`pearson_correlation`'s tail truncation was left alone rather than
hardened into a length check. `tests/risk/test_correlation.py::
test_correlation_aligns_series_on_shared_timestamps` deliberately calls it
with mismatched lengths to demonstrate the *original* positional-alignment
bug, and that test is right to exist. Alignment is the caller's job, as
the docstring says, so the fix belongs entirely in the caller.

### Checked in the same pass and deliberately left alone

* **`correlated_exposure` skips the target symbol.** Adding to an existing
  position in the same instrument therefore does not count the existing
  leg. That is correct: `exposure_limit` already gates on
  `current_exposure + position_notional`, where `current_exposure` is the
  whole book. The correlated check is about *cross-instrument*
  concentration; same-symbol concentration is the plain exposure check's
  job, and counting it twice would double-charge it.
* **Both callers pass `target_notional=0.0`.** `RiskEngine` adds the
  trade's own sized notional itself (`proposal.correlated_exposure +
  position_notional`), so passing the real notional would double-count.
  `app/paper/engine.py` and `app/api/orders.py` both pass 0.0 with a
  comment saying why. No defect.
* **`OptionsRiskProposal` has no correlated-exposure field**, so
  `POST /options/execute` does not consult that gate. Wiring it would be
  inert today: correlation is computed from candle history keyed by
  `Instrument.symbol`, and option contracts have none (`option_chains` /
  `option_snapshots` still have zero writers). Worth revisiting when
  options-chain ingestion exists; adding a gate that can never fire is
  the kind of change the previous rounds were written to avoid.

## A restarted process no longer starts flat (round 107)

`OrderManager` and `PositionManager` live in one process's memory. Every
fill was mirrored into `positions`, and nothing ever read that mirror
back. The note above framed this as a multi-replica concern; it was also,
and more immediately, a **restart** concern on a single replica, and it
reached the risk gates rather than just a stale read.

`current_exposure` and `max_open_positions` are computed by summing the
in-memory book. `correlated_exposure` reads it. `is_reducing` — the
exemption that guarantees a position can always be closed — decides by
looking the position up in it. An empty book makes every one of those
read zero.

Measured end to end on one open position of 100 @ 100, by placing a real
order and then clearing `_STACKS` (which is exactly what a restart does):

| | before | after |
|---|---|---|
| `GET /positions` | `[]` | the position |
| in-memory open positions | 0 | 1 |
| exposure the gates sum | **0.00** | 10000.00 |
| the row in Postgres | open, 100 @ 100 | unchanged |

So a restarted process would have let an account re-take exposure it
already held, and would have treated an exit as a fresh entry.

`app.trading.persistence.load_open_positions` is the inverse of
`persist_position` and is called from `_stack_for` before the stack is
used for anything, and from `AutoTradeSupervisor` when it first builds a
user's manager — that worker had the same gap, with its own
`source_key="auto"` rows. `source_key` is required by the loader for the
same reason `persist_position` requires it: three unrelated managers
mirror into this table and all default to `ExecutionMode.PAPER`, so
loading without it would hand one engine another engine's positions.

### The `Decimal` half, which is a separate failure

The `positions` columns are `Numeric(18, 6)`. SQLAlchemy returns those as
`Decimal` whatever the `Mapped[float]` annotation says, while
`PositionRecord` is float throughout. Restoring raw column values would
put `Decimal` into the book, and the next fill would raise `TypeError:
unsupported operand type(s) for *: 'decimal.Decimal' and 'float'` inside
`apply_fill`'s `average_price * quantity + price * signed_qty`. Hence the
explicit `float()` casts in the loader, and a test that asserts the
restored types directly so those casts are not tidied away later.
`app.risk.portfolio.compute_exposure` already cast at its own read of
these columns for exactly this reason — it was the only place that had
met the problem.

### Orders, and the double fill that exposed

Rehydrating positions but not orders left a sharper bug directly
reachable, found by following that thread. `OrderManager`'s idempotency
index is process memory too, and `persist_order` is idempotent on
`idempotency_key` — it looks the row up by that key and *updates* it,
correctly assuming one key means one order. A process that has forgotten
its keys breaks that assumption. Measured, resubmitting an identical
order after a restart:

| | before | after |
|---|---|---|
| the resubmit | a **new** order id | the original order id |
| position | **100 → 200** | 100, unchanged |
| `orders` rows | 1, showing qty 100 | 1 |

So a second order really filled at the broker while the journal kept one
row for the first — the position could not be reconciled against the
orders that produced it. The key itself is content-derived
(`{user}:{symbol}:{direction}:{entry}:{stop}:{price}`) and was never the
problem; the index holding it was.

`load_recent_orders` rebuilds that index. Two things about it are worth
stating rather than leaving to be discovered:

* **Events are restored, and that is load-bearing.** `persist_order`
  appends `order.events[n:]` where `n` is the count already in the
  database. An order restored with no events would have every subsequent
  transition fall outside that slice and never be written — the audit
  trail would stop at the restart, silently, with no error anywhere.
* **The window is 24 hours, and that is a judgement call.** Within one
  process the index never forgets, but a process lifetime is itself
  arbitrary, and loading an account's whole order history on every stack
  build grows without bound. Every accidental resubmit this dedupe exists
  to absorb — a double-click, a client retry, a user re-pressing after a
  restart — happens within minutes, and a day is the unit the risk
  counters already work in. The residual is real: an identical order
  resubmitted more than a day after the original still creates a second
  order.

## A restart no longer strands a paper session's mirrored position

`POST /paper/{id}/candle` mirrors the paper engine's position into the
`positions` table on every candle under `source_key=f"paper:{session_id}"`,
and `DELETE /paper/{id}` retires that row when the session is discarded.
Both of those go through `_SESSIONS`, a module-level dict in
`app/api/paper.py` with no persistence and no eviction — so a process
restart dropped every live session while their rows stayed in Postgres, and
`DELETE` answered 404 for exactly the sessions whose rows still needed
retiring.

Nothing else could reach those rows. `app/trading/persistence.py` is the
only writer of `Position.is_open` anywhere in `app/`, and reaching it under
a `paper:*` key needed a live engine; a new session gets a new UUID and
therefore a new key. The rows stayed `is_open=True` permanently, and
`app/risk/portfolio.py::compute_portfolio_exposure` sums exactly those
rows. Measured on the real endpoints: one session with one open position,
`GET /portfolio.total_exposure` 15606.06 before a restart, `GET /paper/{id}`
404, `DELETE /paper/{id}` 404, and 15606.06 after — for an account holding
nothing, with no route left that could ever change it. `portfolio_snapshots`
journals the same number.

**This is a reporting fault, not a control failure.** The risk gates on
`POST /orders` and `POST /options/execute` take `current_exposure` from the
in-memory `PositionManager`, never from this table, so an inflated
`total_exposure` was a wrong number shown to the user and stored in the
snapshot journal — it never blocked or admitted a trade that should have
gone the other way.

The fix is that `DELETE /paper/{session_id}` no longer requires the session
to be in memory. Ownership is checked against whichever of the two
representations survives: `engine.account_id` when the session is still
there, exactly as before and as every other `/paper/*` route does, and the
durable rows when it is not — `abandon_position_mirrors` matches on
`user_id` *and* `source_key`, so another user's DELETE retires nothing and
receives the same 404 as a session id that never existed. That helper lost
its `instrument_id` argument in the process (the caller no longer has an
engine to name the instrument) and gained a warning in its docstring: it
now retires every open row under the key it is given, which is safe for a
session-scoped key like `paper:{uuid}` and would retire a whole book if
handed a shared one like `manual` or `auto`.

**The fix is deliberately partial.** The caller needs the session id. The
UI holds it, but a user who has lost it has no route to their own orphaned
rows. The complete fix would be a startup sweep of every open `paper:*`
row — correct on a single replica, where at process start those rows are
orphaned by construction, and destructive on two, where one replica's
orphans are indistinguishable from another replica's live sessions. The
same asymmetry is why the repair path is the one that shipped: it is
addressed at a single session the caller names, so it cannot mistake a
peer's live session for a corpse. On a multi-replica deployment it would
still retire a row belonging to a session live on another replica, which is
what the owner asked for; that replica's engine would keep running and
re-write the row on its next candle.

Three tests cover this in
`backend/tests/api/test_paper_session_cleanup.py`, all of them clearing
`_SESSIONS` to model the restart exactly: the behavioural proof that the
row is retired and exposure returns to 0, a control that another user's
DELETE on the stranded session retires nothing and 404s, and a control that
an invented UUID is still a 404 rather than a blanket 204. Both controls
were measured against injected faults rather than assumed — removing the
`user_id` clause from the lookup fails the first, removing the fallback 404
fails both.

## Client-supplied money has to be money

`POST /replay` bounds its `starting_balance` (`gt=0, lt=1e12`). Its two
siblings did not: `POST /paper`'s `starting_balance` and both
`starting_capital` fields in `app/api/backtest.py` were bare `float`
defaults. A bare `float` in Pydantic accepts `NaN`, `Infinity` and any
magnitude, and `json.loads` accepts the bare `NaN` / `Infinity` tokens, so
no unusual client is needed to send them. The OHLC fields of
`POST /paper/{id}/candle` had the same gap.

Measured on the real endpoints:

* `POST /backtest` and `POST /backtest/validate` with `starting_capital`
  of `1e30`, `Infinity` or `NaN` answered **500** — the `backtests` row
  will not go into `Numeric(18, 6)`. With `-5` or `0` they answered 200 and
  produced a return-on-capital report against an account that cannot
  exist.
* `POST /paper` with `Infinity` or `NaN` answered **500** at session
  creation.
* `POST /paper` with `1e15` answered 200, and the *ninth* candle then
  500ed: the strategy sized the position at 1.5e12 units, `persist_position`
  could not write that, and the session was left with an open position in
  the engine and **zero rows in the journal** — `GET /paper/{id}` showing
  the position while `GET /portfolio.total_exposure` read 0.00, for the
  life of the session. That is the same engine/journal divergence the
  bounds on `PlaceOrderRequest` were added to close.
* `POST /paper/{id}/candle` with a `NaN` close, with a position open,
  answered 200 and left a literal `Decimal('NaN')` in
  `positions.unrealized_pnl` (Postgres `NUMERIC` accepts NaN); the API
  serialised it back as `null`. With `Infinity` it answered **500**.

All of these now answer **422**. The fix is one ceiling, `_MAX_MONEY = 1e12`,
applied the way `POST /replay` already applies it: `Numeric(18, 6)` holds at
most 999999999999.999999, and every number derived from an account balance
here — position quantity, notional, mark-to-market P&L — lands in a column
of that type. `gt=0` does the rest of the work, including on NaN: every
comparison against NaN is False, so a NaN fails the *upper* bound and is
refused. No separate `allow_inf_nan` switch is needed.

A manual paper session's candles come from the client **by design** — the
endpoint is the user driving a simulation, and inventing prices is the
point. The bounds assert only that the numbers are prices at all. Two
things were checked and deliberately left alone: an internally incoherent
candle (`high` below `low`) is inert in this engine — `_maybe_exit`
compares the stop against `low` and the target against `high`, and an
inverted candle satisfies neither — and the retest gate
`candle.low <= entry <= candle.high` already refuses to open on one. No
coherence validator was added for a defect that could not be demonstrated.

**What the ceiling does not do.** It bounds the inputs, not the products.
A balance just under the ceiling with a small enough stop distance still
sizes a position whose mark-to-market P&L overflows `Numeric(18, 6)`. The
control test pins the top of the range that does work (9.99e11 opens,
trades a full round trip, and journals a position of ~1.5e10 units), which
is what keeps the bound from being quietly tightened into uselessness; it
does not prove the column can never overflow.

The tick-to-candle path (`CandleWorker`) has no equivalent guard, and did
not get one here: `normalize_tick` — the function that would validate a
broker's payload — has zero callers in `app/`, and the only feed wired to
the worker is `SimulatedFeed`. There is no live feed to harden yet, and
inventing one to guard it would be speculative.

## Client-supplied strings have to fit the columns they land in

The same shape as the money bounds above, one type over. Postgres does not
silently truncate an over-long value into a `VARCHAR(n)` — it raises
`StringDataRightTruncation` — so a bare `str` request field meant the check
happened in the database rather than at the boundary, and what a client got
back for a bad request was a **500**:

| request | field | column | before |
|---|---|---|---|
| `POST /instruments` | `symbol` at 65 chars | `String(64)` | 500 |
| `POST /instruments` | `exchange` at 33 | `String(32)` | 500 |
| `POST /instruments` | `instrument_type` at 33 | `String(32)` | 500 |
| `POST /instruments` | `currency` at 9 | `String(8)` | 500 |
| `POST /strategies` | `name` at 256 | `String(255)` | 500 |

Two values were accepted that should not have been. An **empty** `symbol`
registered an instrument at 201 — and `symbol` carries a unique index and
is the key every instrument lookup in the system goes through, so the empty
string is a real row that can exist exactly once and matches nothing anyone
would search for. An empty strategy `name` was accepted the same way. Both
are now `min_length=1`.

The last case is a different failure with the same root. A
`StrategyDefinition.timeframe` longer than 8 characters was accepted,
stored, and could then never be evaluated: `candles.timeframe` is
`String(8)`, so no candle row can carry a longer string — measured, 8
characters store and 9 raise — and `ScannerWorker`, `AutoTradeSupervisor`,
replay and backtest all load candles by that exact string. The strategy
simply never fired, with nothing anywhere reporting why. That is the rule
the entry-type (§74) and condition-type (§88) validators already apply, so
it belongs in the same place: the DSL does not accept a strategy the engine
can never satisfy.

`StrategyDefinition.market` is deliberately left unbounded, and a control
test asserts that. It has no reader anywhere in `app/` and lands only in
the JSON `definition` column, so there is no width to match it to and no
failure to prevent; bounding it would be a guess dressed as a rule.

**The known cost of validating in the DSL.** `StrategyDefinition` is
validated on *read* as well as write — `model_validate(row.definition)`
runs every time a stored strategy is loaded — so a row written before this
change with a name over 255 characters or a timeframe over 8 would now fail
to load rather than merely never fire. Nothing in the suite or the fixtures
carries one, and the same trade-off was already accepted by the §74 and §88
validators, but it is a real property of where the check lives rather than
an oversight.

## A failed protective-stop cancel no longer gets a second stop placed on top

`ensure_protective_stop` runs after every fill on a live position and keeps
the broker-side stop in step with the position: cancel the old resting
order, place a new one. `cancel_protective_stop` cleared
`position.protective_order_id` **before** attempting the cancel and then
swallowed every exception, on the reasoning that a stop which has already
fired is not an error.

That reasoning covers one of the two ways a cancel fails. The other is a
request that never landed — a timeout, a 5xx, a dropped connection — where
the order is still resting at the venue. The id was already gone, so
nothing referenced that order any more, and the code went straight on to
place a second stop.

Measured against a broker whose cancel raises, over two fills on one
position:

```
fill 1: stop placed            resting SL_M orders = 1
fill 2 (cancel fails):         resting SL_M orders = 2
    qty=100  (the orphan, at the old trigger)
    qty=200  (the replacement)
  position size                = 200
  quantity resting on stops    = 300
  reported problem             = None
```

The result said the position was protected. It was over-protected in the
worst way: both orders fire together when price reaches the trigger, 300
sells against a 200 position, and the account is left **short 100 with
nothing guarding it** — the naked position this module's own docstring
exists to prevent, manufactured by the machinery meant to prevent it. Every
later fill whose cancel fails adds another orphan.

**Telling the two failures apart.** Adapters raise `BrokerError` for both:
Upstox converts an HTTP error to `BrokerError`, and its HTTP-200
error-envelope shape ("Order already complete") arrives as `BrokerError`
too. Matching on message text would be guesswork, so the fix asks the
question that actually decides it — `broker.get_orders()`, surface every
adapter must implement. An order listed in a settled state (filled,
cancelled, rejected, expired, closed), or not listed at all, is gone; a
venue that has forgotten an order is not about to fire it. Anything else,
including a `get_orders` that itself fails, counts as possibly live.

When it may be live, the id is kept — it is the only handle a retry, the
reconciliation worker or a human has on that order — and
`ensure_protective_stop` returns `problem` with `fate_unknown=True` instead
of placing anything. `app/api/orders.py` already halts the account and
raises `RECONCILIATION_REQUIRED` on exactly that signal, so no new
machinery was needed.

**The trade-off, stated plainly.** Not placing the replacement leaves the
position covered only by the stale order — in the measurement above, 100
units of a 200-unit position, at the old trigger price. That is worse
coverage than the two-stop state in the narrow sense of quantity covered.
It is chosen anyway: a partially covered position plus an immediate halt
and an explicit reconcile alert is recoverable, and an unbounded naked
position facing the other way is not. The alternative was never "correct
protection" — it was a different, worse failure that reported success.

**The ordinary case stays ordinary**, and a control test pins it: a stop
that fired makes the venue refuse the cancel with the same exception a
timeout raises, and that is the routine end of every stopped-out trade. It
must not report a problem, keep a stale id, or stand in the way of the next
entry — otherwise the stop doing its job would halt the account every time.

This is live-path code. As everywhere else in this document, it is exercised
against `MockBroker` and a fault-injecting subclass of it, not against a
real venue.

## The stop-loss feature was halting every account that used it

Two features, each correct in isolation, that between them made live
trading unusable.

`app/trading/protective_stops.py` places a position's broker-side stop by
calling `broker.place_order` directly, deliberately not through
`OrderManager`: it is not an order anyone submitted, it carries no
idempotency key of its own, and its lifecycle belongs to the position
rather than to the order journal. The position holds the only reference to
it, in `PositionRecord.protective_order_id`.

`reconcile_orders` (blueprint §75) flags any broker order that is not in
the local order index and whose status is SUBMITTED, ACKNOWLEDGED,
PARTIALLY_FILLED or FILLED as "unknown locally". A resting stop reports
ACKNOWLEDGED. So every protective stop this system placed was, one minute
later, a reconciliation mismatch — and `ReconciliationWorker` responds to
any mismatch by halting the account, writing an audit row and raising
RECONCILIATION_REQUIRED.

Measured end to end: one `POST /orders` with a stop, then one
reconciliation pass:

```
broker order a208abed  type=MARKET  status=FILLED
broker order cc4ab1f2  type=SL_M    status=ACKNOWLEDGED
reconciliation in_sync = False
  order mismatch: Broker order cc4ab1f2... for ACME is unknown locally
```

Resuming a halted account is a deliberate manual admin action (§75, §116),
so the sequence was: place a live entry with a stop, get halted within 60
seconds, have an admin resume it, place the next entry, get halted again.
A stop-loss and live trading were mutually exclusive.

The fix is that `reconcile()` already receives the local positions, so it
collects their `protective_order_id`s and passes them to `reconcile_orders`
as the set of broker orders this system placed on purpose without an
`OrderRecord`. The exemption is exactly those ids:

* a resting stop no position claims is still flagged — that orphan is
  precisely what reconciliation exists to catch, and after the round above
  it is also what a failed cancel deliberately leaves behind;
* an order placed outside this system entirely (by hand at the broker's own
  terminal, say) is still flagged;
* a stop that **fired** is not hidden. The order goes FILLED and is
  exempt, but the consequence — the position is open locally and gone at
  the broker — is reported by `reconcile_positions`, which is the half of
  the report that matters. A control test pins that, so if it ever stopped
  holding, the exemption would fail rather than quietly swallow a closed
  position.

Ids are collected from every local position, not only the open ones: a
position that has just gone flat still holds the id until its cancel is
confirmed, and flagging it in that window would halt the account for a stop
being retired normally.

## The emergency controls only existed in a Redis with no disk

Two controls in this system are deliberately one-way, in the sense that
only a human is supposed to lift them:

* **account halts** (`halt:*`) — set by `ReconciliationWorker` when local
  and broker state disagree, and by `POST /orders` when a position ends up
  with no stop at the broker. Blueprint §75 makes resuming a deliberate
  manual step, and `POST /admin/accounts/{id}/resume` records it with an
  audit row;
* **the kill switch** (blueprint §58) — global, per-account, per-strategy,
  read by `RiskEngine.evaluate` and `evaluate_options_risk` on every
  proposal.

`app/core/redis.py` is the only store for either. And `docker-compose.yml`
gave `postgres` a named volume while giving `redis` nothing at all: no
volume, and no persistence configured, so both controls lived in the
container's ephemeral layer. Recreating that container — an ordinary
deploy — dropped them. An account halted because its positions disagreed
with the broker would start trading again, with none of the audit trail
the manual resume path leaves behind. The asymmetry sat in the same
fifteen lines of one file, which is probably why it went unnoticed: the
service that obviously holds state got a volume, the one that quietly holds
the safety state did not.

The fix is the same treatment `postgres` already had — a named
`redis_data` volume and `--appendonly yes` — and three tests in
`backend/tests/test_deployment_durability.py` that assert the *properties*
(some durable volume, some persistence configured) rather than the exact
spelling, so the deployment can change how it gets there without breaking
them. The third is a control on `postgres`, so that if a future change
strips both, the failure does not read as though only Redis mattered.

**What this does not establish.** There is no Docker in this environment,
so the container restart itself has never been exercised here — the
evidence is the compose file and the code paths, not an observed restart.
And AOF plus a volume only covers the ordinary restart: a genuine Redis
data loss (a wiped volume, a fresh instance, a failover to an empty
replica) still lifts every halt silently, because the halt exists nowhere
else. The durable fix is to keep halts in Postgres and treat Redis as a
cache of them, which changes how the control is modelled rather than how it
is deployed; it is not taken here.

## Revoking a session now ends its open streams, not just its requests

Blueprint §69's revocation was made real earlier: `get_current_user` looks
the session up on every REST request, so `POST /auth/sessions/{id}/revoke`,
`logout` and the refresh-reuse containment all cut access immediately.
WebSockets kept their own copy of that check — at the handshake, once — and
nothing re-checked afterwards.

Measured end to end, with a socket open on `/ws/orders`:

```
received before revoke: {'event': 'before revocation'}
revoke -> HTTP 204
REST with the same token -> 401
socket still streaming after revoke: {'event': 'AFTER revocation'}
```

`/ws/orders` and `/ws/positions` carry that user's live order and position
events. Someone revoking a session on a stolen laptop or a shared machine
is told the session is gone, is shown it refusing REST — and the socket
keeps delivering, for as long as it stays connected.

`_relay` now runs a third task alongside the forwarder and the
disconnect watchdog: whichever finishes first ends the connection — the
client goes away, the channel errors, or `_watch_for_revocation` finds the
session gone. `_authenticate` returns the session id with the user so
there is something to re-check, and every authenticated endpoint passes it
through; `_relay`'s parameter stays optional, and a control test covers the
no-session caller.

**Two honest limits.** The re-check is a timer
(`SESSION_RECHECK_SECONDS = 30.0`, named rather than inlined so the cost is
visible: one indexed lookup per open socket per interval), so a revoked
session keeps receiving for up to that long — bounded, where it used to be
unbounded. And it polls because revocation is a Postgres `UPDATE` on
`user_sessions` with no event to subscribe to; publishing one on the
existing Redis bus would be tighter and is a larger change than this hole
warrants.

The tests shorten the interval rather than waiting it out — what is being
proven is that the socket closes at all, not the production cadence. Both
were measured against injected faults: a watcher that returns immediately
fails the live-session control, and one that never returns fails the
revocation proof. Note that the control cannot run at all against the
pre-change code, since the constant it pins does not exist there.

## Kill zones were read in the caller's timezone, not UTC

`KillZone` declares `start_hour_utc` / `end_hour_utc`, and
`app/ict/killzones.py` opens by calling them UTC windows. `active_kill_zones`
then read `candle.timestamp.hour` — the hour in whatever zone the timestamp
happens to carry. One instant, two spellings, two answers:

```
2026-01-05T03:45:00+00:00  ==  2026-01-05T09:15:00+05:30   (Python: True)
  as UTC  -> ['ASIAN']
  as IST  -> ['LONDON']
```

IST is the natural spelling for an NSE client, and `POST /paper/{id}/candle`
takes the timestamp straight from the request body, so nothing unusual is
required to hit it. Across one NSE session, three of four sampled times came
out in the wrong zone:

| NSE clock | reported | actual |
|---|---|---|
| 09:15 IST | LONDON | ASIAN |
| 11:00 IST | — | — |
| 13:00 IST | NEW_YORK | LONDON |
| 15:15 IST | LONDON_CLOSE | LONDON |

This is not a display detail. `ConditionType.SESSION`
(`app/strategy/evaluator.py`) matches a strategy's session condition against
`ICTContext.current_kill_zones`, so a strategy restricted to the London kill
zone was firing during the Asian session — silently, and only for clients
who send their own timezone.

`_utc_hour` normalises before the comparison. A **naive** timestamp is read
as UTC explicitly rather than handed to `astimezone()`, which would assume
the *machine's* zone: the same bug one layer down, and one a UTC-configured
CI could never catch. A control test moves the process `TZ` to
`Asia/Kolkata` and asserts a naive timestamp still reads as UTC, so that
shortcut fails the suite rather than passing it.

**Checked and deliberately left alone.** Two neighbours look similar and are
not the same defect. `detect_opening_ranges` compares a caller-supplied
`session_open` against `ts.replace(hour=…)`, which is consistent by
construction: the session open is expressed in the same zone as the candles,
and the function never claims UTC. `bucket_start`'s weekly anchor reads
`timestamp.weekday()` the same way, and its inputs come from Postgres, where
the column is `TIMESTAMP WITH TIME ZONE` and values arrive aware and in UTC.
Neither contradicts its own declared units the way the kill zones did.

There was no test module for ICT kill zones before this; there is one now,
`backend/tests/ict/test_killzones.py`. Note that one of the four sampled
session times (11:00 IST) falls in no zone under either reading, so that
parametrised case is a control rather than a proof — the other three fail
on the pre-change code.

## The opening range was anchored five and a half hours late

The round above left a note saying `detect_opening_ranges` is
zone-consistent by construction: it compares a caller-supplied clock time
against each candle's own, so the two only have to agree. That is true of
the **function**, and it was not true of the **default feeding it**.

`ICTConfig.session_open` defaulted to `time(9, 15)` — the NSE open, written
in IST — while every candle in this system comes from Postgres, where the
column is `TIMESTAMP WITH TIME ZONE` and values arrive in UTC. Measured on
one NSE day of 5-minute candles stamped in UTC, with the opening fifteen
minutes deliberately made the day's extremes:

```
ICTConfig().session_open = 09:15:00
  detected opening range: high=150.0 low=140.0
     starts at 2026-01-05T09:15:00+00:00 = 14:45 IST
  the real opening 15 minutes: high=200.0 low=100.0 starting 09:15 IST
```

09:15 UTC is 14:45 IST — the middle of the afternoon session. And none of
the five `ICTConfig()` construction sites in `app/` passes this field, so
every opening range the system produced was the wrong bars.

The field is now `session_open_utc`, defaulting to `time(3, 45)`, which
*is* 09:15 IST. The rename is the point as much as the number: the sibling
module spells its bounds `start_hour_utc`/`end_hour_utc`, and the unit
being invisible at the use site is what let an IST literal sit in a UTC
slot. A deployment trading anything but NSE has to set it — there is no
session calendar here to derive it from.

**Severity: reporting, not a trade gate.** `opening_ranges` is read by
`app/api/charts.py`'s ICT serialiser (shared with replay analysis) and by
the AI proposal context. No `ConditionType` reads it, unlike the kill
zones, so this was a wrong number on a chart and a wrong input to an AI
suggestion — not a wrong entry.

`backend/tests/ict/test_opening_range.py` is new; the module had no tests.
Two of its cases are controls that pass in both directions and exist to
keep the diagnosis straight: handing the function an IST clock time
against UTC candles still picks the 14:45 bars, and IST-stamped candles
still want an IST session open. If someone "fixes" the function to convert
zones internally, those fail — which is the correct outcome, because the
function's contract is the one thing here that was never broken.

## A `premium_discount` condition with no zone (§33-34)

`Condition.zone` is optional, and the DSL accepted
`{"type": "premium_discount"}` with nothing in it. The evaluator's arm for
that type ends in `condition.zone is not None and ...`, so the zone-less
form is false on every candle forever. Measured directly: with a live
dealing range and `current_zone='PREMIUM'`, the zone-less condition
returned `False` where `zone='premium'` returned `True`.

What makes it worth a guard rather than a docstring is that conditions AND
implicitly (`evaluate_conditions`). One unsatisfiable condition does not
narrow a strategy — it zeroes it. The strategy still validates, stores,
lists and backtests like any other; it simply never produces a signal, and
nothing anywhere says why.

Every *other* condition type reads an omitted optional as "any": an `fvg`
with no `direction` matches a gap either way, an `order_block` likewise, a
`session` with no name matches any kill zone. So there were two coherent
readings — make `premium_discount` match the others, or refuse the form.
Refusing it is the ruling, chosen deliberately: it is the same call
`_reject_unfed_condition_types` makes for condition types no data source
feeds (§88's fix) and `_reject_unknown_entry_types` makes for `entry.type`
— fail at authoring time rather than look alive and do nothing. Treating
the omission as "any zone" would instead invent a meaning the author never
wrote, in the one place where the field *is* the comparison. `POST
/strategies` now returns 422 naming the field.

A blank or whitespace-only zone is refused on the same grounds; it is the
same absence with a different spelling.

**Deliberately not widened:** a *misspelled* zone (`zone="premuim"`) is
still accepted, and still fails closed. That is not an oversight — it is
the choice `_validate_bias`'s docstring already records for free-text
`side`/`zone` values, and overturning it is a separate decision from
filling this gap. `backend/tests/strategy/test_dsl.py` pins it as a
control: tightening the validator to a zone allow-list breaks that test,
which is the signal that a wider decision is being made.

One existing test changed.
`test_lookback_defaults_follow_the_events_formation_time[premium_discount]`
built a zone-less condition purely to read its `lookback` default, so the
new validator rejected it. The test was
wrong, not the change: `zone` has no bearing on the lookback table, and the
test now passes `zone="premium"` with a comment saying why.

## Which address the rate limiter keys on (§105)

`app/core/rate_limit.py` guards `/auth/login` (10/minute) and
`/auth/register` (5/minute) per client IP, and took that IP from
`request.client.host` — the socket peer. With nothing in front of the
process that is the real client, which is why this looked right. Deployed
the way this repo documents — `infrastructure/nginx/nginx.conf.example`,
blueprint §105 — the peer is nginx, the same address for everybody, and
the per-IP limiter becomes **one global bucket for the whole platform**.

Measured before the fix, twelve requests from twelve distinct client
addresses through one proxy, under the login limit of 10/minute:

```
[200, 200, 200, 200, 200, 200, 200, 200, 200, 200, 429, 429]
```

The eleventh and twelfth users could not log in. Not the eleventh attempt
by one abuser — the eleventh *person*. On a trading platform that is a
denial of service anyone can trigger deliberately, and that eleven
ordinary users trigger by signing in at the open.

`X-Forwarded-For` is now consulted, but only when `TRUSTED_PROXY_HOPS`
says a proxy is actually there, and never from the left. nginx's
`$proxy_add_x_forwarded_for` *appends* the peer it saw, so the chain reads
`<whatever the client sent>, <what each proxy observed>`: everything
forgeable is on the left, and counting back from the right by the number
of proxies you run lands on the address the innermost trusted proxy
actually saw. Reading the leftmost entry — the usual shortcut — would be
worse than the bug: any client could pick its own bucket to evade the
limit, or claim a victim's address and lock them out of login. The tests
pin that direction explicitly, with one abuser forging a fresh address per
attempt and still being limited.

The default is `0`, which keeps a proxy-less deployment behaving exactly
as before. That is deliberate rather than timid: a process with no proxy
in front cannot distinguish a real `X-Forwarded-For` from one a client
invented, so trusting it by default would hand every client the evasion
above. The consequence is that this is a fix an operator must *switch on*
— so `.env.example`, `docs/PRODUCTION_READINESS.md` and the nginx example
itself all now say to set it, and the nginx example says why.

**Blast radius is exactly the limiter.** `request.client` had one reader
in the whole backend, so nothing else was attributing requests to the
proxy — no audit row, no session record, no log line. Worth stating
plainly, because "the app is blind to client IPs behind a proxy" sounds
like it should be much larger than it is.

`backend/tests/test_core_rate_limit.py` is new; the module had no tests.
The three end-to-end cases re-enable rate limiting for one throwaway app
(the suite disables it globally, since every test request shares one
client "IP"), and the control among them is the one that must still
reject: a limiter that never says no would pass the headline test for
entirely the wrong reason.

## What a broker writes, and the columns that hold it (§59-63)

Every string a broker hands back is diagnostic text of no promised
length. `UpstoxBroker._extract_error_message` falls back to
`response.text` — the entire body — when the reply is not JSON, and a
proxy or CDN 502 in front of the broker returns an HTML page. Postgres
`VARCHAR(n)` does not truncate: it raises `StringDataRightTruncation` and
takes the transaction with it. So a verbose broker made the write itself
the failure.

Measured, two live sites:

- A **789-character rejection reason** made `POST /orders` raise out of
  `persist_order`. The order journal was left with `[]` — no row at all,
  so no status, no reason, and no record that a live order had been
  refused. An identical retry failed identically.
- A **996-character stop rejection** was worse. The entry had already
  filled and was journaled `MONITORING`, the broker had refused the
  protective stop, the account had been halted — and the write that
  failed was the **notification telling the account holder their live
  position has no stop-loss** (`Notification.body` is `String(1000)`).
  The position was live, unprotected, and unannounced, on precisely the
  path §107/§108 built to make sure it never is.

`app/core/text.py`'s `clip` fits such text to the column and marks where
it cut, applied at the persistence boundary — `persist_order` for
`rejection_reason`, `create_notification` for `title` and `body` — so
every producer is covered rather than each adapter separately. The limits
are read off the columns (`Order.__table__.c.rejection_reason.type.length`
and friends) rather than restated, because the bug underneath all of this
is two places disagreeing about one width. Losing the tail of a
diagnostic message does not compare to losing the message, the order row,
and the alert; `data` is a JSON column with no width, so the
machine-readable half of every notification survives whole.

**An identifier is the opposite case, and got the opposite fix.**
`positions.protective_order_id` was `String(64)` while
`orders.broker_order_id` — the same value, an id the broker issued — is
`String(128)`. Measured: the order journal stores a 100-character id, and
`persist_position` rejects the same string. Clipping would be actively
dangerous here: a shortened order id is not a shorter name for the order,
it is one that silently matches nothing at the broker, which is how a
protective stop becomes invisible to reconciliation. The column is
widened to 128 by migration `d1f3a7c9b204` instead. The narrow
declaration came from §107's own migration — it was a guess, and the
wrong one.

**Deliberately untouched:** `RiskEvent.reason`, also `String(500)`. Its
writers are this system's own risk-engine strings (`decision.reason`,
`outcome.risk_rejected_reason`), bounded by construction, not broker
text. Clipping there would be speculation, not a fix.

Also worth recording, since the test says so out loud rather than
silently: a **broker** rejection on `POST /orders` produces no
notification at all. The endpoint notifies on a *risk-engine* rejection
only. Whether a broker-side rejection deserves one is a real question and
a separate one; this round only made sure the order row survives to be
the record.

## A broker connection that cannot trade (§50-53, §120)

`resolve_broker` picks the most recent ACTIVE `BrokerAccount` for every
order a user places. An account it cannot build a working adapter from
therefore does not degrade that user's trading — it stops it. Both of
these were accepted at **201 ACTIVE** and then broke every subsequent
order with a 500:

- `{"broker": "DHAN", ...}`. `DhanBroker` is a documented skeleton: every
  method raises `NotImplementedError`, because per blueprint §51/§120 it
  has to be written against Dhan's *current* official API rather than a
  guess baked into this repo. Measured: `POST /orders` returned
  `NotImplementedError: TODO: implement using Dhan's market quote / LTP
  endpoint`, from several frames below the endpoint, every time.
- `{"broker": "UPSTOX", "credentials": {"nope": "x"}}`. Here
  `resolve_broker` already *had* the diagnosis — it raises
  `BrokerError("Connected Upstox account <id> has no access_token
  stored")` on purpose. `_stack_for` simply called it with no handler, so
  that sentence died in a traceback and the client got a 500.

The resolver's own docstring explains why it refuses rather than falling
back to `MockBroker`: blueprint §101, never make paper and live look
identical — "a user who connected a broker and gets an error knows
something is wrong". The reasoning was right and the delivery was
missing.

Two layers, because they cover different people:

- **The connect boundary** turns away what cannot trade — DHAN with 501,
  an Upstox connection with no `access_token` with 422 naming the key —
  and stores no row, since a stored ACTIVE row is the thing that breaks
  the orders. Same ruling as the DSL's refusal of conditions nothing can
  satisfy and entry types the engine cannot resolve: fail where the
  person can still act on it.
- **`resolve_broker` and `_stack_for`** cover accounts already stored
  before that guard existed. The resolver now refuses DHAN outright
  instead of handing back a skeleton, and `_stack_for` turns any
  `BrokerError` into **503** carrying the resolver's message — 503 rather
  than 502 because the broker did not fail; this process cannot talk to
  it at all. That covers `/orders`, `/positions`, `/portfolio` and
  `/options/execute`, all of which build their stack through it.

**Not done here: implementing the Dhan adapter.** That needs Dhan's
current API, which this environment cannot reach, and the adapter's own
docstring plus §120 are explicit that guessing at it is the wrong move.
This round makes the gap legible instead of fatal.

**One existing test asserted the opposite** — that a Dhan account
resolves to a `DhanBroker` — and it did, which was the bug: the assertion
pinned the handover of an object whose every method raises. It is now
`test_active_dhan_account_is_refused_rather_than_handed_a_skeleton`, with
the history in its docstring.

No wedged orders, and the PR says so: the Dhan failure happens in quote
validation, *before* the order is registered under its idempotency key,
so repeated attempts kept failing cleanly rather than poisoning that
order forever.

## The Sortino ratio's denominator (§48)

`compute_metrics` built Sortino's denominator as `_std` of the *losing*
returns — the sample standard deviation of the losses about their own
mean, i.e. how much the losses differed from each other. The Sortino
ratio is defined against the **downside deviation**: the root-mean-square
shortfall below the target, over every observation. Those are different
questions and give different numbers. Below two losing trades the code
switched to a third formula again, `abs(r)`.

Measured against the textbook value on a 100,000 account:

| run | reported | correct |
|---|---|---|
| three wins, three identical −2,000 losses | `None` | 1.0607 |
| a single losing trade | 1.0 | 2.0 |
| losses of differing size | 0.8729 | 0.7127 |
| three identical losses, no wins | `None` | −1.0 |

The first row is the one that matters. `RiskLimits.risk_per_trade_pct`
sizes every position so that a stop costs the same fixed fraction of the
account, so **equal-sized losses are the normal shape for this platform's
own strategies** — and that is exactly when the spread of the losses is
zero and the old form reported the ratio as undefined. A strategy doing
precisely what the risk engine asks of it was the case the metric could
not describe.

`_downside_deviation(values, target=0.0)` replaces both branches. It
divides by N, every observation, not by the count of losing ones: a
winning period genuinely contributes zero downside, and dividing by only
the losers would make a strategy look better the more often it won, which
inverts the metric. That also keeps it comparable with `sharpe`, which
divides by the same N. The N versus N−1 difference from `_std` is
deliberate — this is an RMS about a fixed target, not a dispersion
estimated about a sample mean. The denominator still yields `None` rather
than an infinity when nothing fell below the target, for the reason §68
records: `backtest_metrics.sortino` is `Numeric(10, 4)`, and the replay
statistics beside it go into a Postgres `json` column that rejects the
bare `Infinity` token.

**Severity: reporting.** `sortino` is stored and served; it gates
nothing. But it is a number a person reads when deciding whether to trade
a strategy, and it is half of what §37's out-of-sample validation puts
side by side. No test asserted either ratio before this round, which is
how it survived.

`sharpe` is untouched and now has a control test pinning it as the
ordinary sample-deviation ratio over all returns, so a future change
cannot quietly give the two ratios one denominator.

**Negative results from the same survey**, recorded so they are not
re-run. The kill switch *is* reloaded on the autonomous path — the
supervisor drives `PaperTradingEngine`, which refreshes it every candle.
Look-ahead is clean: every `SMCEngine.analyze` caller passes a sliced or
growing candle list, and `detect_swings` cannot emit an unconfirmed swing
from a truncated slice by construction, which leaves `visible_swings`
dead but harmless. `OptionChainSnapshot`, `OptionContract` and
`OptionSnapshot` have no writers anywhere; both readers handle the
absence, and the options-execute path tells the caller "no liquidity data
available — not evaluated" rather than pretending. That last one, and the
0.0 defaults for `market_data_age_seconds` and `premium_deviation_pct`,
are the documented "honestly degraded" choice and belong to the open
question about whether *no* market data should fail closed — not settled
here.

## Real market data, paper execution (§14, §52)

The requirement is real Upstox prices with every execution path staying in
paper. Taken literally that is not what connecting Upstox gives you:
`resolve_broker` routes every order to the most recent **ACTIVE**
`BrokerAccount`, and `_execution_mode_for` stamps anything that is not a
`MockBroker` as LIVE. So storing an Upstox token as a broker account in
order to read prices would also send that user's real orders to Upstox.
The `LIVE_TRADE` permission is a second gate, but the two controls are not
independent the way the requirement needs.

So market-data credentials are **process-level config**
(`UPSTOX_DATA_ACCESS_TOKEN`), create no `BrokerAccount` row, and are
consumed only by `app/market/providers/upstox.py`'s `UpstoxMarketData` —
which is deliberately **not** a `Broker`, does not subclass it, and has no
`place_order`, `modify_order` or `cancel_order`. The guarantee is
structural rather than a convention someone has to remember: there is no
method on the object that could reach an exchange. `resolve_broker` keeps
returning `MockBroker`, and execution stays PAPER.

**The credential is still a trading credential.** Upstox issues no
read-only market-data token, so this value can place orders through any
other client. What this module guarantees is that *this system* offers no
path from it to an order — not that it is safe to leak.

Two conversions at the boundary are load-bearing, and both fail silently
rather than loudly:

- **Order.** Upstox returns candles newest-first. Every consumer here —
  `detect_swings`, `bucket_start`, the paper engine's `candles[-1]`, the
  backtest loop — assumes oldest-first. A reversed series raises nothing;
  it just produces confident nonsense. Sorted ascending at the boundary.
- **Timezone.** Upstox stamps candles `+05:30`. This codebase is UTC
  throughout, and two separate regressions already came from an IST clock
  time reaching a UTC reader (§111 kill zones, §112 the ICT session open).
  Converted at the boundary so nothing downstream has to know Upstox
  exists.

The timezone test initially asserted only the instant, and **passed with
the conversion removed** — `09:15+05:30` and `03:45+00:00` are the same
instant, and aware datetimes compare by instant. An injection run caught
that. It now asserts the offset and the hour (3, not 9), which is what
actually matters: `app/market/aggregation.py`, `app/smc/liquidity.py` and
`app/ict/opening_range.py` all read `.hour`/`.date()`/`.weekday()`
straight off the timestamp.

**Not verified against live servers.** This environment cannot reach
upstox.com, so the endpoint paths and response shapes are written from
Upstox's documented v2 forms — the same caveat `app/brokers/upstox/
adapter.py` already carries, and the reason §120 says to implement against
current official docs. Parsing is isolated in `parse_candles` / `parse_ltp`
so it is fixture-tested now and correctable from one real call later,
without touching the transport code around it.

**Still missing before a real backtest can run:** nothing resolves
`Instrument.symbol` ("NIFTY") to the `instrument_key` Upstox requires
("NSE_INDEX|Nifty 50"). `Instrument` has no column for it, so no live call
can currently name an instrument. That is the next piece.

## Naming an instrument to a market-data provider (§14, §52)

`Instrument.symbol` is a trading symbol — "INFY", "NIFTY". Upstox names
instruments as `"NSE_EQ|INE009A01021"` or `"NSE_INDEX|Nifty 50"` and
accepts nothing else; its adapter's own rollout checklist has said so
since it was written. Nothing in this codebase translated between the two,
so **no market-data call could name an instrument at all** — the client
added alongside it had no way to be pointed at anything.

`instruments.broker_instrument_key` (nullable, indexed) holds it.
Nullable because it is provider-specific and absent for every row until a
master file is loaded — an instrument without one is still perfectly
usable for paper trading and for backtests on candles that arrived some
other way.

`resolve_instrument_key` turns the absence into a legible failure naming
the instrument and the remedy, rather than returning the plain symbol as a
fallback. A symbol Upstox does not recognise comes back as an opaque
rejection several layers from the cause; this is the same ruling §116
makes for a broker account no adapter can be built from. `backfill_candles`
resolves **before** it calls the provider, so a missing key costs no
request.

Matching is **case-insensitive on both sides**, and that is not
fastidiousness: Upstox's master is not consistent with itself. Equities
carry an upper-case `trading_symbol` ("INFY") while indices carry a
title-cased one ("Nifty 50"). Exact matching silently misses every index,
which on an NSE-focused platform is most of what anyone wants to trade.

`apply_instrument_keys` returns an `InstrumentKeyReport` whose `unmatched`
list is the point of the type. An instrument the master has no entry for
stays unusable for market data, and that has to be visible to whoever ran
the load — a silent partial success is discovered later as a backfill that
returns nothing and a strategy that never fires. A stored key is **not**
overwritten by default either: it was loaded from an earlier master or
corrected by hand, and re-running a load should not quietly replace it.

### The interval that must not be approximated

Upstox serves `1minute`, `30minute`, `day`, `week`, `month`. This
codebase's `SUPPORTED_TIMEFRAMES` includes `5m`, `15m`, `1h`, `4h`, none
of which Upstox offers — they are *derived* from 1m by `resample_candles`.

So `backfill_candles` takes **this codebase's timeframe** and derives the
provider interval itself; there is no parameter through which a caller
could hand in a mismatched pair. A timeframe Upstox cannot serve is
refused, naming the ones it can and pointing at the aggregation path.

Approximating instead — fetching `1minute` and storing the rows under
`"15m"` — is the failure mode this exists to prevent, and it is entirely
silent: the candles store, the backtest runs, every number it produces is
wrong. An injection that made `upstox_interval_for` fall back to
`"1minute"` is caught by four tests.

Writes go through `upsert_candles`, which is a genuine upsert, so a
backfill re-run over an overlapping range is idempotent rather than a
`uq_candle_key` crash.

**Still missing before real candles can actually land:** the master file
has to be fetched. That is one HTTP GET this environment cannot make, so
parsing and matching are pure functions over already-loaded records and
the download is the caller's small problem. Nothing here has been
exercised against Upstox's real master or its real candle payloads.

## Where a candle bucket begins (§14, §16)

Two things here, one fixed and one deliberately left open.

### `bucket_start` gave the same input two different answers

A naive timestamp took whichever path the bucket size chose. The weekly
branch does no timezone arithmetic and silently returned a bucket; the
sub-weekly branch subtracts an aware epoch and raised `TypeError: can't
subtract offset-naive and offset-aware datetimes`. Measured:
`bucket_start(datetime(2026, 9, 16, 9, 15), 15)` raised while the same
value at `10080` returned `2026-09-14`.

A naive timestamp is now read **as UTC**, which is the convention this
codebase has already settled twice — §111's `_utc_hour` and PR #137's
Upstox candle parser both do the same, and both record why `astimezone()`
is the wrong call: on a naive value it assumes the *machine's* zone, which
a UTC-configured CI can never catch. Both branches now agree, and every
bucket start comes back UTC-aware whatever went in, so a naive bucket
timestamp can no longer flow back into candle data.

Production was never affected: the DB column is `TIMESTAMP WITH TIME
ZONE` and the Upstox parser guarantees aware timestamps. This was fixtures
and any future caller.

### `resample_candles` emits a partial trailing bucket, and says so now

Eighteen 1m candles resampled to 15m give one 15-minute bar and one
3-minute bar — and nothing distinguishes the second from a closed one.
`detect_swings`, the strategy engine and every `candles[-1]` read it as
finished. `CandleWorker` avoids exactly this on the live path, and
deliberately: it only aggregates once `_completes_bucket` confirms the next
base candle falls outside the bucket.

`resample_candles` cannot make that check, and the reason is worth stating
rather than papering over: whether more bars are coming is not something
the input can say, and a market's closed periods make it undecidable from
wall-clock alone — a weekly bar built from Monday-to-Friday dailies is
complete in trading terms and short of its wall-clock end. So the caller
has to know, and the docstring now says so instead of leaving it to be
discovered.

**This was reachable because of PR #138.** `resample_candles` has *no
production caller at all* — `CandleWorker` uses `aggregate_candles` plus
its own completeness check — and #138's backfill pointed operators at it
as the way to derive 15m from 1m without mentioning the tail. That
docstring now sends them to the note first. The hazard is bounded and
self-correcting in practice (`upsert_candles` overwrites, so the next
overlapping fetch fixes the bar), but a strategy evaluating in between
sees a forming bar as closed.

### Open: intraday bars are anchored to midnight UTC, not to the session

Epoch anchoring makes every sub-weekly size align with midnight UTC, and
`bucket_start`'s docstring used to call that "correct for them" because
they divide a day evenly. The arithmetic is right and the conclusion
overreaches: aligning with midnight is not aligning with a *session*.
Measured against the NSE open at 03:45 UTC (09:15 IST):

| size | open lands |
|---|---|
| 3m, 5m, 15m | on the boundary |
| 30m | 15 min into the bucket |
| 1h | 45 min in |
| 2h | 105 min in |
| 4h | 225 min in |

So at 30m and above the first bar of an NSE day is a stub — an "hourly"
candle holding fifteen minutes of trading and forty-five of nothing.
Whether an intraday bar on this market should instead be anchored to 09:15
IST is a semantics decision about what these bars *mean*, not a defect to
be silently corrected, and it would move every existing stored bar at
those sizes. Left to the operator; a parametrized test records the current
answer so that changing it is a visible choice rather than a drift.

Worth noting for the Upstox path specifically: `30m` is one of the two
intraday timeframes #138 maps to a provider interval Upstox serves
natively. If Upstox anchors its own 30-minute bars to the session, the
same `"30m"` timeframe would mean different things depending on whether a
bar was fetched or derived. Unverified — this environment cannot ask
Upstox — and worth checking with the first real fetch.

## A quiet minute and the derived candle it costs (§16, §66)

`CandleWorker._derive_timeframe` writes a higher-timeframe candle only
when its bucket holds exactly `window` base candles — fifteen 1m bars for
a 15m one. The worker is tick-driven, so **a minute in which nothing
traded produces no base candle at all**, and the bucket comes up one
short. Measured:

```
every minute traded (0..14)   -> 15m candle written: YES
minute 7 had no trades        -> 15m candle written: NO
```

This is the only writer for derived timeframes and it only fires on the
tick that completes the bucket, so that 15m bar is missing permanently.
`ScannerWorker` and `AutoTradeSupervisor` both run at 15m, so the hole
lands directly under the strategies, and `detect_swings` then reads a
discontinuous series as if it were continuous.

Nothing in the suite noticed because `SimulatedFeed` emits exactly one
tick per candle, with no gaps — the shape of test data hid a property of
real data.

### Why the skip stays, for now

The obvious fix — require the bucket's *opening* slot instead of a full
count, so later gaps read as quiet minutes — was written, measured
working, and **reverted**. It breaks
`test_derive_timeframe_skips_an_incomplete_bucket_instead_of_corrupting_the_prior_one`,
which records skipping a gapped bucket as a deliberate choice.

That test is right to exist, and the conflict is real rather than
accidental: **this layer cannot distinguish a minute with no trades from a
minute whose ticks were lost.** Both leave no base candle. The two
readings trade off against each other —

- *assume quiet, derive anyway*: correct for an illiquid instrument, where
  gaps are constant; wrong during a feed outage, where it publishes a bar
  built from partial data without saying so.
- *assume loss, skip* (today): correct during an outage; loses bars
  routinely on anything thinly traded.

Neither is strictly right, and the choice changes what the strategies see.
That makes it the operator's call, not one to take silently while they are
away. Worth noting the reverted fix did **not** reintroduce the
positional-slicing bug that test was originally written for — the previous
bucket stayed intact under it; only the skip-versus-derive policy moved.

### What did change: the skip is no longer silent

Whichever policy wins, dropping a bar the strategies depend on should be
observable. It was a bare `return`: no log line, no metric, no error, and
no way for an operator to learn their 15m series had holes. It now logs a
warning naming the instrument, the bucket and how many of the expected
base candles were found, and increments
`derived_candles_skipped_total{timeframe}`. Non-zero and climbing on an
instrument means its higher timeframes are incomplete.

A control test asserts the warning fires on the gap and on nothing else —
a warning that also fires on the happy path is one an operator learns to
ignore.

## A backtest position open at the last candle (§46-48, §77-78)

`BacktestEngine.run` kept the position it was holding in a local
`open_trade` and returned only completed trades. A run that opened a
position and still held it when the data ran out therefore returned `[]`,
which is the same value a strategy that never fired returns, and
`compute_metrics` then reported `total_trades: 0` over it.

Measured before the change, with a stub strategy that fires once on a
flat 10-candle series whose stop and target are never touched:

```
candles fed           : 10
closed trades returned: 0
engine.open_trade     : (did not exist)
```

The bias runs one way. A strategy whose stop is wide enough that the
window ends before price reaches it has exactly those trades omitted,
while the trades that did close — including its winners — are counted.
Nothing about the omission is symmetric, and §77-78's out-of-sample
comparison reads the same numbers off three consecutive splits, so every
split's trailing position is dropped the same way.

`ReplayEngine` never had this problem: it has always exposed `open_trade`
as instance state, and both `app/api/replay.py` and
`app/replay/persistence.py` read it. The backtester is the sibling
implementation that kept the same state private.

What changed: `BacktestEngine` now publishes an `OpenBacktestPosition`
(direction, entry, stop, target, quantity, `opened_at`, `last_price`,
`unrealized_pnl`), resets it at the top of every `run`, and logs a warning
when a run ends holding one. `backtests.open_position` (migration
`f3a92b7c5d10`) stores it as nullable JSON, and `POST /backtest` and
`GET /backtest/{id}` both return it as `open_position`, `null` on the
runs — most of them — that end flat.

What deliberately did **not** change: the metrics still cover closed
trades only. `unrealized_pnl` is gross and marked at the final candle's
close — no exit happened, so no exit cost is charged and no fill price is
being claimed. Folding an unclosed position into `total_trades`,
`win_rate` or the equity curve would mean marking it to market and calling
that a result, which is a decision about what a backtest reports, not one
to take quietly underneath an existing report. The position is now
visible; what to do with it is the reader's call.

## The equity liquidity gate had no writer (§40, §56)

`TradeRiskProposal.liquidity_acceptable` defaulted to `True`, and neither
of `RiskEngine.evaluate`'s two callers has ever set it. Checked
mechanically over `app/`:

```
TradeRiskProposal construction sites in app/:
   app/paper/engine.py:373  passes liquidity_acceptable = False
   app/api/orders.py:474    passes liquidity_acceptable = False

RiskEvent.checks recorded for an equity order on a symbol with zero volume:
   liquidity_acceptable = True   (approved = True)
```

So every equity order ever evaluated — manual (`POST /orders`), paper, and
autonomous (`AutoTradeSupervisor` drives the same `PaperTradingEngine`) —
wrote a passed `liquidity_acceptable` row into its `RiskEvent` audit
record without anything having looked at volume, spread or quote age. An
operator reading `GET /admin/risk-events` could not distinguish a symbol
whose liquidity was assessed and found fine from one where the gate was
never wired up. That is a fabricated audit entry, and §56 makes the risk
engine the only authority that can veto a trade — the record of what it
checked has to be true.

The sibling path does it properly: `OptionsRiskProposal.liquidity_acceptable`
is genuinely computed in `app/api/options.py` from `OptionSnapshot` rows
through `evaluate_liquidity` (`app/options/liquidity_filter.py`), and a leg
with no snapshot produces an explicit "no liquidity data available — not
evaluated" warning. The equity engine is the sibling that declared the
same field and never fed it.

`liquidity_acceptable` is now `bool | None`, defaulting to `None` for "no
assessment was made", the same distinction `market_data_age_seconds`
already draws between "fresh" and "no data at all" in the same dataclass.
`None` is **skipped** rather than recorded as passed — precisely what
`is_reducing` already does for the entry-only limits, so the audit row
lists only the checks that actually governed the decision. It does not
reject: a gate nobody has wired up must not block trading, which is the
same choice `max_correlated_exposure_pct`'s 100.0 no-op default makes.
A caller that *does* assess liquidity passes a real bool and gets a real
gate — `False` rejects, on reducing orders too, since an illiquid symbol
is illiquid whichever way the order goes.

One existing test changed with it. `test_a_reducing_proposal_skips_only_the_entry_limits`
asserted `liquidity_acceptable` was among the checks a reducing order
always runs. That expectation encoded the bug rather than a requirement:
the name appeared in every decision because the default put it there, not
because anything had been assessed. The check still runs, and is still on
the execution-sanity side of the fence, whenever a caller supplies an
assessment; there is a test pinning exactly that.

What this does **not** do is give equities a liquidity gate. Nothing yet
computes one. `evaluate_liquidity`'s thresholds are open-interest and
option-spread shaped, and what a minimum traded volume should be for an
NSE equity is a number to choose deliberately against real data, not to
invent in passing — so it is left for the operator to decide, and until
then the audit row says nothing rather than something false.

## A worker could die without anything noticing (§54, §66, §117)

Two defects, one failure. `app/core/redis.py`'s `heartbeat` is a bare
`redis.set` with no error handling, and both worker loops called it
**outside** the `try` that guards each pass:

```python
while True:
    try:
        await self.run_once()
    except Exception:
        logger.exception("Auto-trade pass failed")
    await heartbeat("auto_trade")      # <- outside the guard
    await asyncio.sleep(self.interval_seconds)
```

Measured by injecting one `ConnectionError` into `heartbeat`:

```
ScannerWorker          run() DIED: ConnectionError   (passes completed: 1)
AutoTradeSupervisor    run() DIED: ConnectionError   (passes completed: 1)
```

A single transient Redis blip — a failover, a restart, a dropped
connection — raised straight out of `while True` and ended the task after
one pass. The call whose entire job is to report that a worker is alive
was the call that killed it.

Nothing then noticed. `app/workers/main.py` created each worker with
`asyncio.create_task` and sat in `await stop_event.wait()`, which only
SIGINT/SIGTERM ever sets:

```
task 'autotrade' done=True  stop_event set=False
main() would still be sitting in `await stop_event.wait()`
at shutdown, gather(return_exceptions=True) hands back:
    [ConnectionError('Redis went away for a moment')]
...and main() discards it without looking.
```

So the process stayed alive and the container stayed up with the worker
gone, and the cause was collected at shutdown and thrown away without
ever being logged. For unattended autonomous trading (§54) that is the
failure mode you must not have quietly: the account simply stops trading,
with open positions still held, and the only symptom is a heartbeat that
stopped — from the one code path that was already broken.

Both halves are fixed. The heartbeat now sits in its own guard in all
three loops (`ScannerWorker.run`, `AutoTradeSupervisor.run`, and
`_bridge_market_data_to_candles`), so a Redis error is logged and the loop
continues; a pass that fails is still tolerated exactly as before, and
still heartbeats, because the loop is in fact alive. And `main` now wraps
each worker in `_supervise`, which logs a worker that ends — **raised or
simply returned** — and restarts it after a bounded backoff (5s, doubling
to 60s), standing down as soon as `stop_event` is set.

The plain-return case is the one no exception handler would ever have
caught: `_bridge_market_data_to_candles` ends by returning when its feed
stops yielding, which is exactly what a dropped market-data WebSocket
looks like from inside that loop.

Restarting in-process rather than exiting is deliberate, and it is the
one place this had to choose. Exiting is the tidier answer for a
supervised container — but `docker-compose.yml` sets no `restart:` policy
on **any** service, so a clean exit would turn a recoverable fault into a
permanent outage. Adding that policy is a deployment decision, not one to
take inside a bug fix, so it is left for the operator; the backoff is also
why the restart delay is not zero, since the shipped
`SimulatedFeed(candles_by_symbol={})` returns immediately and a zero-delay
restart would spin it into a hot loop.

One thing measured and then deliberately not changed: `_supervise` catches
`Exception`, never `BaseException`. `asyncio.CancelledError` derives from
`BaseException` precisely so a broad handler cannot swallow it, which is
what makes shutdown work. An explicit `except asyncio.CancelledError:
raise` was written first and then removed once injection showed it was
dead code — it could not change any outcome. The test that guards this
asserts on a bounded wait rather than a bare `await`, because a supervisor
that did swallow cancellation would hang the suite instead of failing it.

## The API process had the same worker bug, and it broke shutdown too (§66, §75)

The previous section fixed three loops and missed a fourth.
`app/trading/live_reconciliation.py` had the identical unguarded
`await heartbeat("reconciliation")` inside its `while True`, and one
injected `ConnectionError` ended it after a single pass:

```
run() DIED: ConnectionError: Redis went away for a moment   (passes completed: 1)
```

An AST sweep of every `heartbeat` call site now confirms there is exactly
one class of these and it is closed:

```
GUARDED   app/trading/live_reconciliation.py
GUARDED   app/workers/auto_trade_worker.py
GUARDED   app/workers/main.py
GUARDED   app/workers/scanner_worker.py
```

This copy was worse than the worker ones on three counts.

It runs **inside the API process**, so nothing looked ill afterwards: the
API kept serving requests and `GET /health` kept reporting the process up.
What died is §75's order-divergence safety net — the loop that notices a
live broker's order state no longer matches the local journal and halts
the account.

And the dead task then broke shutdown. `lifespan` cancelled the task and
awaited it under `except asyncio.CancelledError: pass` — but cancelling a
task that has *already finished* does nothing, and awaiting it re-raises
whatever it stored. The original `ConnectionError` propagated out of
teardown, skipping the engine and Redis disposal immediately below it,
which exists precisely to stop connections leaking. One transient Redis
blip therefore cost the safety net, its own visibility, and the cleanup
that would have contained it.

Three changes. The heartbeat is guarded, as in the workers. `lifespan`
now wraps the loop in `supervise`, so a loop that ends is logged and
restarted rather than silently gone. And teardown catches `Exception`
alongside `CancelledError`, logging it — `supervise` should make that
unreachable, but teardown is the wrong place to discover it did not.

`supervise` itself moved from `app/workers/main.py` into
`app/core/supervision.py`. That is the point of this round rather than a
tidy-up: there were two entrypoints with the same shape, one was fixed and
the other was missed, and a second copy would have gone the same way. Its
`stop_event` is now optional, because the API process has no SIGINT/SIGTERM
event to pass — it shuts the loop down by cancelling the task, which works
for exactly the reason the handler catches `Exception` and never
`BaseException`.

One test written for this round was **vacuous and was rewritten**. The
first version planted its own already-dead task beside the lifespan's and
asserted teardown completed; injection showed it passed with the
`except Exception` clause removed, because `lifespan` awaits the task *it*
created, not one standing next to it. The replacement substitutes
`supervise` itself, so the task lifespan holds is the dead one, and it
then fails against that injection. It deliberately asserts on teardown
completing rather than on a log line: the app configures its own logging,
and pinning the message would test that configuration instead of this
behaviour.

## A Redis outage made login a 500 (§69, §72)

`check_rate_limit` is a bare `redis.incr`/`expire` with no error handling,
and the `rate_limit` dependency did not catch it. Measured through the
real app with Redis refusing connections:

```
POST /auth/login      -> 500   (unhandled ConnectionError)
POST /auth/register   -> 500   (unhandled ConnectionError)
```

500 is the wrong answer whichever way the policy goes. It tells the client
this service has a defect when the truth is that a dependency is
unreachable, and `app/core/middleware.py` counts an unhandled exception
into `http_requests_total{status_code="500"}` — so a Redis blip lands in
the metric an operator watches for real bugs, on the two endpoints most
likely to be hit while they are trying to diagnose the outage.

The dependency now catches it, logs which way it went, and answers **503
with `Retry-After: 5`**:

```
POST /auth/login      -> 503   Retry-After=5
POST /auth/register   -> 503   Retry-After=5
```

**The policy is now a setting rather than an accident.** `rate_limit_fail_open`
defaults to `False`, which keeps exactly what the 500 did — deny — so this
change fixes the status code without quietly weakening brute-force
protection. Both readings are defensible and neither is free:

* **Deny** (default): nobody logs in while Redis is down, *including the
  operator*, who needs the admin endpoints to resume halted accounts and
  lift the kill switch — and both of those live in Redis too.
* **Allow**: login stays up, and brute-force protection is gone for the
  duration, which is precisely when an attacker who caused the outage
  would want it gone.

Which one this deployment should run is a decision for the operator, not
one to take inside a bug fix. It is left at the conservative default and
raised explicitly.

What this does **not** change: the limiter's behaviour when Redis is
healthy. Under the limit it allows, over it still returns 429, and there
is a control test pinning that neither the 503 nor the fail-open branch is
involved on that path.

## /health died during the outage it exists to report (§72, §117)

Continuing the previous section's probe — other bare Redis calls in
request paths — `check_workers` calls `worker_is_alive`, which is a bare
`redis.exists`, and nothing caught it. Measured with Redis refusing
connections:

```
check_database   -> DOWN
check_redis      -> DOWN
check_workers    -> RAISED ConnectionError
GET /health      -> 500
```

Its two siblings degrade politely; this one raised, and took the whole
endpoint with it. That is the worst possible moment for `/health` to die:
it is what a load balancer polls, what an uptime monitor pages on, and the
first thing an operator opens during an outage — and the `redis: DOWN`
line it would have shown is the entire explanation. The intent was clearly
graceful degradation, since `check_database` already catches and the
Redis ping is already guarded; one of the three was left unwrapped.

`check_workers` is now guarded and reports every worker DOWN. **DOWN is
the honest answer, not a new "unknown" state.** The contract this function
already has is "no heartbeat observed in the last 30 seconds", and an
unreachable heartbeat store is exactly that: no evidence of life.
Reporting HEALTHY would be a claim nothing supports — there is a test
pinning that, because a guard that returns HEALTHY would have been the
easy mistake.

The control matters as much as the fix: with Redis reachable, a worker
that has heartbeated must still read HEALTHY while one that has not reads
DOWN. A guard that flattened everything to DOWN would have traded a 500
for a permanently useless answer, and an injection doing exactly that
fails two tests.

This closes the probe. Every `heartbeat` call site is guarded (previous
section), every `asyncio.create_task` in `app/` is supervised, and the
remaining bare Redis calls are in paths where an outage already fails
safe: the kill-switch and account-halt reads raise, which stops trading,
and the autonomous supervisor's per-pass guard logs and skips. That is the
correct direction for a risk gate, and it is left alone deliberately.

## The ICT engine recomputed session levels and dropped them (§21, §54)

This one is a **performance fix, not a correctness fix** — no output
changes — and it is worth stating that plainly before the numbers.

`ICTEngine.analyze` filled `ICTContext.session_levels` with
`detect_session_levels(candles, "day") + detect_session_levels(candles, "week")`
on every call. Nothing read it. Checked across `app/` and `tests/`:
`app/strategy/evaluator.py` reads `current_kill_zones`,
`GET /charts/{id}/smc` exposes the kill zones and the opening range, and
`app/ai/context_builder.py` reads the kill zones — none of the three
touches `session_levels`.

`SMCEngine.analyze` computes the identical pools into
`SMCContext.liquidity_pools`, which *is* what the evaluator reads. Measured
on the same 2000-candle series:

```
ICTContext.session_levels               : 46 pools (PREVIOUS_DAY/WEEK_HIGH/LOW)
SMCContext.liquidity_pools, same kinds  : 46 pools
identical set                           : True
```

The cost, measured by toggling it off:

```
  500 candles  0.70ms -> 0.38ms   (45.6% of the call)
 2000 candles  3.02ms -> 1.61ms   (46.8%)
 6000 candles  8.70ms -> 4.35ms   (50.0%)
```

`AutoTradeSupervisor` runs one engine per (user, strategy, instrument) and
calls `ict_engine.analyze` on every pass of the 60-second loop, so this was
roughly half the ICT budget spent producing a value that was then
discarded. Rounds on the quadratic SMC loops established that this loop's
latency budget is a real production property, which is why it is worth
removing rather than leaving as harmless clutter.

Removed: the computation, the `session_levels` field, and
`ICTConfig.enable_session_levels` — whose only effect was to gate the
removed work, so leaving it would have been a switch wired to nothing.
Anything wanting this data reads `smc.liquidity_pools` and filters on
`LiquiditySourceType.PREVIOUS_DAY_*` / `PREVIOUS_WEEK_*`.

The duplication is pinned by **call count rather than by a timing
assertion**, which would be flaky in CI: one `ICTEngine.analyze` +
`SMCEngine.analyze` pass must detect session levels exactly twice (day and
week), not four times. An injection that keeps computing the value and
merely stops storing it fails that test and no other, which is what makes
it the real proof. A second test pins the property that makes removal safe
— the same pools are still reachable from the SMC context — and fails when
the removal is done on the wrong side.

## The position P&L math, probed and guarded (§60) — no bug found

**A clean result, stated as one.** This round found no defect; it is
recorded because "we checked and it holds" is worth as much as a fix when
the thing checked is the money math under every risk gate.

Two property probes, each 20,000 random cases:

* `PositionManager.apply_fill` / `mark_to_market` against a cash-flow
  ledger that cannot be wrong by construction (cash paid and received,
  plus the mark value of whatever is still held) — **0 mismatches** across
  random long/short fill sequences.
* `CostModel` across random brokerage / slippage / spread / tax
  configurations — **0 cases where friction improved P&L**, and slippage
  is directionally correct on both sides: a long pays more on entry and
  receives less on exit, a short the reverse.

What *was* thin is coverage. The only assertions behind this math were two
hardcoded numbers in `tests/trading/test_execution.py` —
`average_price == 25000.0` and `realized_pnl == 100.0` — both for a single
long round trip. Nothing covered the short side at all, which is half of
what the autonomous loop trades. The probe is now a seeded test, reduced
to a CI-sized 400 sequences, plus explicit short-side cases.

Recorded so nobody assumes otherwise: `apply_fill`'s **direction-flip
branch is not reachable in production today**. `POST /orders` computes
`is_reducing` itself from the open position and clamps an opposing order
to `min(quantity, abs(existing.quantity))`, so it can only reduce or
flatten; the paper engine closes with the exact open quantity. The branch
is defensive, and the test exercises it directly rather than through a
caller that cannot produce it. That was checked, not assumed — the first
reading of it was wrong.

Because these tests assert behaviour that was already correct, a
stash-verify proves nothing and injection is the only honest measurement.
Eight injections were run; five are recorded in the PR, and three deserve
mention here because they were aimed at the tests rather than the code:

* dropping `mark_to_market`'s `is_open` guard changed **nothing**, which
  exposed the first version of this file's control as vacuous;
* so did a rewrite of that control asserting a mark leaves `realized_pnl`
  alone — a flat position has quantity 0, so `(price - average) * 0` is 0
  whatever else is broken, and no assertion about marking a flat position
  can fail;
* the control was replaced with one about **sign** — a losing trade must
  report a negative realized P&L on both sides — and an injected `abs()`
  in the realized calculation fails it.

That is the fourth vacuous test caught by injection in this sequence of
rounds, and the reason the discipline is worth its cost: a green tick on a
test that cannot fail is worse than no test, because it reads as coverage.

## The backtest report, probed and guarded (§48) — no bug found

**The second consecutive clean result**, and worth saying plainly: four
probes in a row have now come back clean, which is itself a finding about
where this codebase stands.

`compute_metrics` was probed over 20,000 random trade sets, every reported
number recomputed independently from the same trades: **0 discrepancies**.
The equity curve starts at the starting capital and ends at capital plus
net profit with length `total_trades + 1`; `max_drawdown` equals the worst
peak-to-trough on that same curve; `monthly_returns` partitions the P&L
exactly; and no ratio ever goes non-finite.

Route authorization was probed the same way: every route in
`app/api/admin.py` requires `require_admin`. The one result that looked
alarming — `POST /trading-permissions/grant` guarded only by
`get_current_user` — is **by design, checked not assumed**: the module is
documented as self-service, requires `confirm: true`, writes an audit row,
and exists because `POST /auto-trading/enable`'s `require_permission` gate
otherwise had no way to ever be satisfied. It is an opt-in against
*accidental* activation, not an authorization boundary against the account
holder, who is trading their own account.

What was thin, again, is coverage. Before this file the only assertions on
`compute_metrics` output were `win_rate == 1.0` and the equity curve's
length. `max_drawdown` — the headline number someone reads to decide
whether a strategy is safe to run — had none at all, nor did the drawdown
curve, `monthly_returns`, `expectancy` or `average_r`. The risk ratios are
covered separately and deliberately not repeated here.

Because these tests assert behaviour that was already correct, injection
is the only honest measurement:

```
drawdown sign inverted                          -> 2 fail
peak tracks last equity, not the high-water mark -> 2 fail
monthly buckets overwrite instead of accumulate  -> 1 fail
equity curve omits its starting point            -> 3 fail
profit_factor returns infinity again             -> 2 fail
```

The control was injection-tested too, not just the proofs — the lesson
from four vacuous tests caught earlier in this sequence. The last
injection is the shape of a real bug this codebase already had: an
infinite `profit_factor` cannot be stored in `Numeric(10, 4)`, and in the
replay path it wedged a whole session for exactly as long as the user was
winning.

## Nothing restarted a dead container, and two carried questions settled (§54)

**One real deployment gap, and two open questions closed on merit.** The
gap is small in diff and large in consequence; the two decisions changed
no behaviour at all, and are recorded here because leaving them open was
itself becoming a cost.

### A dead process stayed dead

`docker-compose.yml` set no `restart:` policy on any service. The
in-process supervisor added earlier (`app/core/supervision.py`) brings a
dead *loop* back inside a living process, and that is where most of the
failure modes live — but it can do nothing about the process itself. An
OOM kill, an unhandled exception escaping `main`, a segfault in a C
extension, or simply a host reboot left the API or the worker down until
a human noticed. For a system whose entire premise is running unattended
overnight and through a session (§54), "until a human notices" is the
wrong answer, and it is worth naming the inversion: `supervise` restarts
loops in-process partly *because* nothing outside would have restarted
the process. That is a workaround for a missing policy, not a substitute
for one.

`restart: unless-stopped` now covers `postgres`, `redis`, `api` and
`worker`. `unless-stopped` rather than `always` so that a deliberate
`docker compose stop` survives a host reboot instead of being undone by
it.

`migrate` deliberately gets **no** policy, and the asymmetry is the whole
reason this is not a blanket setting. It runs `alembic upgrade head`
once, and both `api` and `worker` wait on it with
`service_completed_successfully`. A restart policy there would turn a
failed migration into a restart loop that never completes, so the two
services waiting on it would never start at all — strictly worse than
the failure the policy on them prevents.

Both properties are asserted in `tests/test_deployment_durability.py`,
on the shape of the configuration rather than its exact spelling, so a
deployment can change *how* it restarts without failing this spuriously.
Measured by injection, because the code under test is configuration and
a stash proves nothing:

```
restart policies stripped (the original state)     -> 1 fail
a restart policy added to the one-shot migrate job -> 1 fail  (the control)
only the worker loses its policy                   -> 1 fail
```

The control was injection-tested alongside the proofs, not assumed.

Still not a substitute for exercising the real thing: there is no Docker
in this environment, so no container has ever actually been killed and
watched to come back. What is verified is that the configuration says to.

### `market_data_age_seconds is None` rejects — decided, not defaulted

Carried for several rounds as an open question in `TradeRiskProposal`.
It is now settled: **`None` rejects, and should.**

A live order is sized from a price, and the notional gates are computed
from that price. With no feed for the symbol there is nothing against
which to say our view of the market is current — and "we don't know how
stale this is" is not the same claim as "it is fine". The paper engine
and anything trading against `MockBroker` pass `0.0` explicitly rather
than `None` (`_market_data_age_for` in `app/api/orders.py`), so the
rejection lands only where it should: a live order on a symbol the
market-data worker is not following.

The cost is real and is the right cost. Live trading on a symbol now
requires the market-data worker to be running *for that symbol*. That is
a precondition to satisfy, not a limitation to design around. No code
changed — the behaviour was already this, and is already covered at
`tests/risk/test_engine.py` — what changed is that it is now a decision
with a reason rather than an accident nobody had ruled on.

### Rate limiting fails **closed** — decided

When the 500-on-Redis-outage bug was fixed, `rate_limit_fail_open`
defaulted to `False` explicitly to keep the security posture unchanged;
the bug being fixed was the traceback, not the policy. The policy is now
decided on its own merits, and the answer is the same: **deny**.

The limiter's job on `/auth/login` is to blunt credential stuffing.
Failing open during a Redis outage hands an attacker precisely the window
they would engineer if they could — take out the shared cache, then
brute-force unthrottled — which turns an outage from a nuisance into an
amplifier.

The counter-argument, that an operator could be locked out of their own
system exactly when they need to intervene, is real but weaker than it
looks. With Redis down, `account_halt_reason` and the kill switch cannot
be read either, so the admin actions someone would log in to perform do
not work regardless. The remedy for "Redis is down" is to restore Redis,
which the `restart: unless-stopped` above now does without a human. A
deployment where login availability genuinely outranks brute-force
protection can set it `True`, and should know that is the trade it is
making.

## Derived candles are built from what traded (§16) — reversing an earlier call

**A real correctness bug, and one this codebase introduced itself.** Round
119 fixed a genuine corruption in `CandleWorker._derive_timeframe` — it
was picking base candles *positionally*, so a gap let it aggregate across
two buckets and overwrite the previous, already-correct bar. That fix
(select by bucket timestamp) was right and still stands. The other half
of that change was not: when the selected bucket held fewer base candles
than the period has minutes, it wrote **no bar at all**, on the reasoning
that a partial bucket might misrepresent its period.

That reasoning was wrong, and this reverses it.

### Why the partial bar is the correct bar

An OHLCV bar is defined over what traded: the open is the period's first
traded price, the high and low its extremes, the close its last, the
volume its sum. A minute in which nothing traded supplies none of those.
So the aggregate of the minutes that *did* trade is not an approximation
of the bar — it **is** the bar.

Measured directly. A 15-minute bucket was built whose minute 7 never
traded, and the bar computed from the raw ticks was compared against the
aggregate of the fourteen base candles that exist:

```
ground truth from raw ticks : open=100.47 high=101.93 low=98.02 close=98.80 volume=3429
aggregate of 14 base candles: open=100.47 high=101.93 low=98.02 close=98.80 volume=3429
IDENTICAL
```

The old code threw that bar away. Which is worth stating plainly: on an
illiquid instrument — exactly the kind where an untraded minute is
routine — the higher timeframes were being thinned out of a series that
was available and correct.

### Why the hole was worse than a partial bar

The rare case is different: ticks genuinely lost. There the bar *is*
short of data, and dropping it was meant to be the conservative choice.
It is not, for two reasons.

It **amplifies**. One lost minute became a fifteen-minute hole — the
damage multiplied by the ratio of the timeframes rather than confined to
the minute that was lost. And the 1m series keeps its own gap regardless:
the worker writes the surviving 1m bars without hesitation, so refusing
to derive from them was never consistent with how the same data is
treated one timeframe down.

And the hole is **not inert**. Nothing downstream reads timestamps for
adjacency: `detect_swings` and the indexed SMC detectors walk the list
positionally, so a missing bar silently welds two non-adjacent periods
together. Measured over a 300-bar series, dropping any single bar changed
`detect_swings` output in **82 of 290 positions** — with real swings
vanishing and swings appearing that never happened. A bar built from
fourteen minutes instead of fifteen is a much smaller error than a
fabricated swing, and `ScannerWorker` and `AutoTradeSupervisor` both read
15m, which Upstox does not serve directly and which therefore only ever
comes from this derive path.

### What it does now

Derive whenever the bucket holds any base candle; write nothing only when
the whole period had no trades at all — a bar needs an open, and an open
is a traded price. That empty case is defensive rather than reachable:
`_derive_timeframe` runs from `_on_candle_closed`, which has just
committed a base candle lying inside the very bucket being derived.

`derived_candles_skipped_total` is renamed to
`derived_candles_incomplete_total`, exported name included. It no longer
counts bars refused; it counts bars built from partial data, and keeping a
metric called "skipped" for something no longer skipped would be a label
that lies about what it measures. Nothing scrapes it yet, so there are no
dashboards to migrate. Climbing on one instrument still means the same
thing it always did — either that instrument barely trades, or ticks are
being lost — and from inside the worker those remain indistinguishable.

### On the tests that had to change

Two existing tests asserted the old behaviour (no bar written), so they
were rewritten rather than deleted, and it is worth being explicit about
which side was wrong: **the tests were right about what the code did and
wrong about what it should do.** The property they were really guarding —
that an incomplete bucket must not corrupt the bar before it — is
unchanged and still asserted, now alongside the assertion that the current
bucket gets its own bar at its own timestamp. The new bar is checked
against the ticks that were actually fed rather than hand-copied
constants, so it has to match the period it claims to describe rather than
merely be non-empty.

Injection, since several of these assert behaviour rather than a stash-able
diff:

```
old behaviour restored (skip the incomplete bar)   -> 2 fail
aggregates the lookback window, not this bucket    -> 1 fail
incomplete bucket no longer counted                -> 1 fail
every bucket counted as incomplete                 -> 1 fail  (control)
empty-bucket guard removed                         -> 1 fail  (control)
log still says the bar was not derived             -> 1 fail
```

Both controls were injection-tested alongside the proofs.

## Intraday bars are anchored to the session open (§16) — a latent trap, closed

**A latent trap, said plainly: this changes nothing that runs today.** It
is here because the trap is real, cheap to remove, and was getting more
expensive to leave — not because anything is currently producing wrong
output from it.

`bucket_start` anchored every sub-weekly bucket on the Unix epoch, which
puts its boundaries on midnight UTC. That is not the same claim as
aligning with a *session*, and the difference shows up as soon as the bar
is larger than 15 minutes:

```
size  | midnight-UTC anchored                      | session anchored
  15m | 25 bars, first=03:45 (15m)                 | 25 bars, first=03:45 (15m)
  30m | 13 bars, first=03:30 (15m of 30) last full | 13 bars, first=03:45 (full) last 15m
   1h |  7 bars, first=03:00 (15m of 60) last full |  7 bars, first=03:45 (full) last 15m
   4h |  3 bars, first=00:00 (15m of 240)          |  2 bars, first=03:45 (full) last 135m
```

The day's first "4h candle" was stamped **00:00 UTC = 05:30 IST** — a time
no NSE instrument has ever traded in — and held fifteen minutes of trading
out of a nominal two hundred and forty.

### The honest version of the trade-off

Neither anchoring gives every bar its full period. An NSE session is 375
minutes, and 30, 60, 120 and 240 all fail to divide it, so **exactly one
bar a day is short whichever anchor is chosen.** The decision is only
about *where that bar sits*, and it is worth stating that rather than
pretending the change makes the arithmetic come out even.

Session anchoring is the better place for three reasons:

* A bar stamped before the market opened names a period that never
  existed. A short *final* bar is stamped at a real trading time, and is
  what every market with an odd session length already produces.
* The opening range is the most information-dense part of an NSE day.
  Burying it inside a bar that is mostly closed market is the worst place
  to lose resolution.
* The codebase already declares the session opens at 03:45 UTC —
  `ICTConfig.session_open_utc`, fixed in an earlier round when it was an
  IST literal handed to UTC candles — and anchors the opening range on it.
  The candle grid disagreed, so a strategy combining an opening-range
  condition with 30m structure was reading two different clocks. Those two
  declarations now have a test asserting they agree.

### Blast radius, measured rather than asserted

Production derives only 5m and 15m (`app/workers/main.py`) and buckets
ticks at 1m. 225 divides all of those, so the offset is a **no-op for
every timeframe in the live path**, and there is a parametrised control
comparing 1m/3m/5m/15m against the old midnight-UTC formula directly, so
that claim fails loudly if it ever stops being true. `resample_candles`
has no production caller at all.

What this closes is the trap waiting for whoever first adds "30m" to
`derived_timeframes` or resamples to "1h": they would get a first bar of
the day stamped before the market opened, and — if Upstox's own
30-minute bars are session-aligned, which this environment cannot reach
their servers to confirm — a stored series in which backfilled and derived
bars interleave fifteen minutes apart instead of coinciding, since
`upsert_candles` keys on `(instrument, timeframe, timestamp)` and would
treat them as different bars rather than the same one.

Daily and weekly keep calendar anchoring, deliberately. A 00:00–24:00 UTC
day already contains the whole NSE session, so an offset there fixes
nothing and would restamp every stored daily bar; weekly keeps the Monday
anchor from an earlier round. Both are asserted as a control, because the
offset being intraday-only is the part a later change is most likely to
"tidy up".

The anchor is a named constant with an optional parameter rather than a
literal, on the same footing as `ICTConfig.session_open_utc`: a deployment
trading anything but NSE has to set it, and a 24h market wants zero. There
is no session calendar here to derive it from.

Where the tests are concerned, one existing parametrised test asserted the
old alignment. It was written in the round that found it, deliberately, to
"state the current answer so that changing it is a choice" — so it failed
here exactly as intended, and stating the new answer is the choice it was
there to force.

Injection, since anchoring is arithmetic with no stashable defect:

```
midnight-UTC anchoring restored (the original state) -> 5 fail
offset added where it should be subtracted           -> 9 fail
offset applied to daily buckets too                  -> 1 fail  (control)
session open moved off the ICT one                   -> 6 fail  (control)
offset used to index but never added back            -> 8 fail
```

Both controls were injection-tested alongside the proofs.

## The correlated-exposure gate now reads direction (§85) — latent, and measured as such

**A latent bug: the gate is inert at the shipped defaults, so nothing has
been wrongly rejected in production.** That was probed, not assumed.
`max_correlated_exposure_pct` and `max_exposure_pct` both default to
100.0, and correlated exposure is by construction a *subset* of gross
exposure, so the correlated check cannot fail unless `exposure_limit`
already has. Over 20,000 random books: **0 cases** where the correlated
gate failed while the gross one passed.

It becomes live the moment an operator tightens it — which is the only
configuration in which the check does anything at all, and exactly when a
risk control should be trusted.

### What it got wrong

Direction never reached the gate. Both call sites built
`{symbol: abs(quantity) * average_price}`, `correlated_exposure` took
`abs(notional)` again, and pairs were matched on `abs(corr) >= threshold`.
Measured with the limit tightened to 60%:

```
long TGT + long SIB,  corr +0.95 (one bet)   -> REJECT
long TGT + SHORT SIB, corr +0.95 (a hedge)   -> REJECT
long TGT + long SIB,  corr -0.90 (a hedge)   -> REJECT
SHORT TGT + long SIB, corr +0.95 (a hedge)   -> REJECT
```

All four identical. Three of them are hedges, and the gate was blocking
the trade that *reduced* the risk — a risk control inverted into a risk
generator.

The inverse-correlation half is indefensible under any reading. Matching
on `abs(corr)` deliberately catches instruments that move *opposite* to
each other, and then the code counted them as though they moved together.
Either they belong in the sum with the opposite sign, or they should not
be matched at all; adding them positively is the one option that is wrong
both ways.

### Why netting is safe here, and where it would not be

The honest objection to netting is real: an estimated correlation can
break exactly when it is being relied on, and a book that nets to zero
must not therefore be allowed to grow without bound. That is how hedged
books blow up.

It does not apply here because netting is not the only limit.
`RiskLimits.max_exposure_pct` caps **gross** notional, reads
`current_exposure`, which stays unsigned at both call sites, and is
untouched by any of this. Gross and net are two limits doing two different
jobs; this one is the net one, and its job is concentration of
*directional* risk. There is a control asserting exactly that: a perfectly
hedged book clears the concentration gate and still fails a tightened
gross limit.

### The sign convention

Everything is expressed relative to the target instrument's price going
up, and the contribution of each correlated position is
`sign(correlation) × signed_notional`:

```
position long,  corr +0.9 -> target up, it gains -> +notional
position short, corr +0.9 -> target up, it loses -> -notional
position long,  corr -0.9 -> target up, it loses -> -notional
position short, corr -0.9 -> target up, it gains -> +notional
```

The proposed trade joins the sum with its own direction, which the engine
reads from the stop's position relative to the entry — unambiguous by the
time the check runs, since `evaluate` has already rejected `entry == stop`
several checks earlier. The **magnitude** of the total is the
concentration; the sign only says which way the book leans, and taking
`abs()` is what keeps a short-leaning book from producing a negative
percentage that slips under every positive limit.

### One dict comprehension, written twice, that no test could catch

Injection found a real hole in this round's own tests before it found
anything else. Re-adding `abs()` to **both** call sites —
`app/api/orders.py` and `app/paper/engine.py`, whose comments already said
the two must mirror each other — left the entire suite green, because
nothing drove either path with a short position and correlation data at
the same time.

So the comprehension is now one function, `signed_notionals_excluding`,
with one test. That is not tidying: it is the difference between an
invariant that is stated and one that is enforceable. Re-running the same
injection against the shared helper now fails.

Injection, after that fix:

```
correlated_exposure sums abs() again (the original state)  -> 4 fail
correlation sign inverted                                  -> 7 fail
inverse correlations counted as if positive                -> 1 fail
engine drops the abs(), a short book goes negative         -> 1 fail  (control)
engine ignores the proposed trade's own direction          -> 2 fail
the shared helper abs()es the notional again               -> 1 fail
the helper stops excluding the target's own position       -> 1 fail  (control)
```

Every control was injection-tested alongside the proofs, including the one
asserting that a hedge must still consume its full gross allowance.

## Equities finally have a liquidity gate (§57) — a missing feature, not a bug

**A missing feature, built, and the last of the seven parked decisions.**
Nothing here was broken; something was absent, and an earlier round said
so explicitly rather than pretending otherwise.

That round found `TradeRiskProposal.liquidity_acceptable` defaulting to
`True` with no writer, so every equity order's `RiskEvent` recorded a
passed liquidity check that nothing had performed. It made the field
`bool | None`, skipped it when unset, and closed with: *"Wiring that up is
the follow-on; not claiming it happened is this change."* This is the
follow-on.

### Why a rate, not a floor

The question is not "is this instrument liquid" in the abstract. It is
*can this order be filled without the order itself moving the price
against us* — a property of the order and the instrument **together**. So
the measure is the order's quantity as a share of what typically trades in
a bar.

An absolute minimum-volume floor gets both ends wrong, and there is a
control pinning both: it would block a tiny order in a thin name that
would fill fine, and wave through an enormous one in a liquid name that
would not. Injecting a floor in place of the rate fails five tests.

The options module (`app/options/liquidity_filter.py`) deliberately does
not share this. Its thresholds are open interest and bid/ask spread on a
specific contract — facts about the contract, not about the order — and
there is no equivalent for an NSE equity here. Reusing option-shaped
numbers would have been inventing a limit rather than choosing one.

### The number, and what kind of number it is

`RiskLimits.max_participation_pct = 10.0`, and it is worth being exact
about its status: **reasoned, not calibrated.** There is no licensed feed
in this environment, so it is not measured against real NSE volume. It
comes from standard percentage-of-volume execution practice, where
algorithms that deliberately spread an order over time target 5–25%; a
single MARKET order consumes visible depth all at once rather than
spreading, so the low end of that range is the right neighbourhood.
Against a 15m bar it works out near 0.4% of a day's volume — permissive
for anything ordinary, and still catching an order that is large relative
to what the instrument actually trades. It is the first number to revisit
once real data exists.

### Three outcomes, not two

* **`None` — not assessed.** No candle history, no registered instrument,
  no database session, or a database error. The check is skipped, exactly
  as before. Recording `True` here would recreate the fabricated audit row
  this whole line of work started from, so the injection that does it
  fails three tests.
* **`False` — assessed and refused.** Either the order exceeds the cap, or
  the window traded *zero* volume. Zero is not missing data: it is data
  saying nothing traded at all, and a participation rate against zero is
  undefined rather than small.
* **`True` — assessed and fine.**

The window is the last 20 bars — five hours at 15m, most of an NSE
session. Long enough that one unusually quiet or busy bar cannot decide
the verdict, short enough to describe today's market. A proof drives that
directly: an instrument liquid a month ago and thin today is judged on
today, because averaging all history would let a dead name keep trading on
its reputation.

### Ten existing tests failed, and the fixtures were what was wrong

Turning the gate on broke the paper engine's correlation test and the
whole autonomous end-to-end file. Root cause, measured rather than
assumed: those fixtures carry `volume=100.0` — a placeholder from when
nothing read the field — while their setups size to 250–500 shares. They
were asking the engine to buy **250–500% of a bar's entire traded volume
in a single order**, which is precisely what the gate exists to refuse.

So the fixtures were unrealistic, not the gate, and they now carry a named
`LIQUID_BAR_VOLUME` rather than a magic literal, so the assumption is
visible. No test was skipped, weakened or deleted; each still measures
what it says it measures.

### The round-132 lesson, applied and then needed again

The previous round found a shared contract broken at both order call sites
with the entire suite still green. So this round injected the same class
of break deliberately — and **found the gap again**: breaking the *live*
path's wiring left everything passing. The paper path had a proof; `POST
/orders` did not.

It does now, with a control beside it: the same 100-share order is refused
against an instrument trading 100 shares a bar and placed against one
trading 50,000, so the test cannot pass by the gate simply refusing
everything.

Injection, after that:

```
'not assessed' reported as passed (the original bug's shape) -> 3 fail
a window that traded nothing treated as 'no data'            -> 1 fail  (control)
an absolute volume floor instead of a participation rate     -> 5 fail
averages all history, not the recent window                  -> 1 fail
the paper path stops passing the verdict                     -> 1 fail
a database failure reported as passed                        -> 1 fail  (control)
the live path stops passing the verdict                      -> 1 fail
the gate refuses everything regardless of volume             -> 1 fail  (control)
```

All three controls were injection-tested alongside the proofs.

### What it still is not

A participation cap against historical bar volume is not order-book depth.
It cannot see a wide spread, a thin top of book, or an auction; it knows
only what traded, after the fact. That is the honest limit of what this
codebase can assess without a live depth feed, and it is a real
improvement on assessing nothing while claiming otherwise — not a
substitute for the real thing.

## Every options RiskEvent claimed three checks nobody performed (§40, §56)

**A real bug, and one an earlier round of this same work looked straight
at and got wrong.**

An earlier round found `TradeRiskProposal.liquidity_acceptable` defaulting
to `True` with no writer, so every equity order's audit row recorded a
liquidity check nothing had run. Fixing it, that round wrote:

> The sibling `OptionsRiskProposal.liquidity_acceptable` is genuinely
> computed by app/api/options.py from `OptionSnapshot` data
> (`evaluate_liquidity` in app/options/liquidity_filter.py), so it keeps
> its `bool` type.

**That was wrong.** The computation exists; its data source has no writer.
A probe for database columns with no writer anywhere in `app/` turned up
five, and three of them were `OptionChainSnapshot.spot_price`,
`OptionChainSnapshot.fetched_at` and `OptionContract.chain_id` — which led
to the real finding: **nothing in production writes `option_chains`,
`option_contracts` or `option_snapshots` at all.** Only the test suite
inserts them. `_latest_option_snapshot` therefore returns `None` for every
leg of every real call, the `continue` fires, and all three quote-derived
fields keep their defaults.

Those defaults were the everything-is-fine values: `0.0` seconds stale,
liquid, `0.00%` from the market. Measured:

```
options, no data:  liquidity_acceptable PASS  premium_matches_market PASS  market_data_fresh PASS  -> APPROVE
equity,  no data:  liquidity_acceptable SKIPPED (absent)                   market_data_fresh FAIL
```

Same system, two order paths, opposite answers to the same question. And
the options answer contradicts a decision recorded only a few rounds
earlier — that `None` rejects because *"we don't know" is not "it's
fine"*.

### What changed, and what deliberately did not

The three fields are now `| None`, and `evaluate_options_risk` records
each only when it was actually evaluated. An operator reading
`GET /admin/risk-events` can now tell a strategy whose quotes were checked
and were fine from one where the gate was never wired up — which is the
entire point, and was the entire point of the equity fix too.

It deliberately does **not** reject on `None`, and the asymmetry with
equities is the considered part rather than an oversight. For equities a
feed exists, so its absence for one symbol is an anomaly worth stopping
on. Here the absence is total and permanent until an options-chain
ingestion path is built, so rejecting would take a working endpoint
offline to punish a gap in this codebase rather than a fact about the
market. Building that ingestion is the follow-on; not claiming it happened
is this change.

Worth stating plainly, because it is the uncomfortable half: **until that
ingestion exists, `POST /options/execute` has no liquidity gate, no
premium-deviation gate and no staleness gate.** It never did. What it had
was three audit entries saying otherwise.

### The call site, for the third round running

Injection found the same class of hole in this round's own tests that the
two preceding rounds found in theirs. Replacing the line in
`app/api/options.py` that records the per-leg liquidity verdict with
`pass` left the entire suite green: the pure function had proofs, the
endpoint had none. Three endpoint-level tests now drive the real route
against real stored quotes — illiquid, liquid, and unquoted — and assert
on the persisted `RiskEvent.checks`.

That is three rounds in a row where the uncovered half was the wiring and
not the logic. The pattern is worth naming: a shared helper with a tidy
unit test reads as covered, and the thing that actually decides whether a
user is protected is the one line at the call site that nobody asserts on.

Injection, after that fix:

```
the everything-is-fine defaults restored (the original state) -> 2 fail
liquidity recorded even when unevaluated                      -> 17 fail
0.0 staleness skipped as if it were None                      -> 1 fail  (control)
premium check never recorded at all                           -> 4 fail  (control)
the caller stops recording a real liquidity verdict           -> 2 fail
the caller takes the freshest leg's age, not the stalest      -> 1 fail
one illiquid leg no longer condemns the strategy              -> 1 fail  (control)
```

All three controls were injection-tested alongside the proofs.

## A quiet market read as a dead worker (§117)

**A real bug in the health signal, and the codebase's own comment is what
gave it away.**

`_HEARTBEAT_TTL_SECONDS` (90s) carries a careful note explaining how it
was chosen:

> scanner_worker.py, auto_trade_worker.py, and live_reconciliation.py all
> call `heartbeat()` once per 60-second loop […] The TTL must be
> comfortably longer than that.

It names three workers. There are four. `market_data` is missing from that
list because it had no loop interval to name: `heartbeat("market_data")`
sat **inside the tick loop** of `_bridge_market_data_to_candles`, so the
key was refreshed only when a trade happened.

### What that costs, measured against a real session

NSE trades 03:45–10:00 UTC. A subscribed, perfectly healthy worker goes
stale 90 seconds into every silent stretch:

```
every weeknight    1065 silent minutes
every weekend      3945 silent minutes
reported UP at best   1875 / 10080 min = 18.6% of the week
```

So `GET /health` — the endpoint an earlier round specifically hardened for
use *during* an incident — called the market-data worker dead for roughly
four fifths of every week. An operator wiring that to alerting gets paged
every evening; one who learns to ignore `market_data` misses the outage it
exists to show. That is the alert-fatigue failure, and it is a defect in
the signal rather than in the worker.

It is also the mirror image of a bug an earlier round already fixed — a
30-second TTL against a 60-second loop, which made `worker_is_alive()`
flap for the back half of every cycle. Same flap, opposite cause, still
present in the one worker that comment did not enumerate.

### The fix, and the constraint on it

The heartbeat now runs on a 60-second timer, matching every sibling and
the cadence the TTL was chosen against.

The constraint worth stating: **it is tied to the subscription's lifetime,
not the process's.** The beater starts when the bridge subscribes and is
cancelled in a `finally` when the bridge returns or raises, so a genuinely
dead feed still goes stale within one TTL. A heartbeat that outlived the
thing it describes would be the same fabricated claim the two preceding
rounds removed from the equity and options risk paths — a green light
nobody earned.

### A control that hung instead of failing

Injection caught this round's own test file twice before it was right.
Removing the beater's cancellation leaves the bridge awaiting a task
nothing stops, so the test **hung** rather than failing — first on an
unbounded `await`, then again on an unbounded `asyncio.gather` after the
first bound was placed on the wrong call. A control that hangs is killed
by a CI timeout and read as a flake, which is worse than no control at
all.

Every task shutdown in the file is now bounded by
`asyncio.wait([task], timeout=…)` with an assertion naming the cause, so
the same injection now fails in eight seconds. An earlier round recorded
exactly this lesson; it did not transfer on the first try, which is worth
writing down rather than quietly fixing.

Injection, after that:

```
back to beating once per tick (the original state)       -> 2 fail
the beater is never stopped when the feed dies           -> 2 fail  (control)
heartbeat unguarded again, one Redis error ends it       -> 1 fail  (control)
interval raised past the TTL, reintroducing the flap     -> 1 fail  (control)
ticks no longer reach the candle worker                  -> 1 fail  (control)
```

All four controls were injection-tested alongside the proof.

### Found on the way, and not fixed here

The probe that surfaced this — public functions with no caller anywhere in
`app/` — turned up something larger that deserves its own round rather
than being folded into this one:

`app/workers/main.py` constructs `SimulatedFeed(candles_by_symbol={})`, an
**empty** dict, so `subscribe()` yields nothing and the bridge returns
immediately, busy-restarting under `supervise` forever. Separately,
`backfill_candles` — the only path from `UpstoxMarketData` into the candle
store — has no caller in `app/` at all. Every layer of ingestion exists
(provider client, instrument-key resolution, normalization, backfill,
aggregation) and nothing wires any of it.

That is a missing feature, not a correctness bug, and it is the honest
counterweight to this round: fixing the heartbeat makes the health signal
tell the truth, and the truth it will tell in the current deployment is
that there is no market data.

## Real market data can now reach the candle store (§14)

**A missing feature, and the biggest one left.** Nothing here was
computing a wrong answer; there was simply no path from a market-data
provider into the store every strategy reads, and two previous rounds
named it rather than fixing it.

A probe for public functions with no caller anywhere in `app/` found it.
Most hits were FastAPI route handlers — framework-called, a false-positive
class — but two were not, and one of them mattered: **`backfill_candles`
had no caller at all.** The function existed, the read-only
`UpstoxMarketData` client existed, `resolve_instrument_key` existed. Every
layer of ingestion was present except the one that would have used them,
so the only candles a deployment could ever hold were whatever a test had
inserted.

### What it does now

`POST /admin/backfill` — admin-gated, on-demand, idempotent. It is
deliberately modelled on `POST /admin/portfolio-snapshot` rather than made
a background loop: the range to fetch is a judgement call, provider
history is rate-limited, and a real deployment should drive it from its
own scheduler. `upsert_candles` is a real upsert, so re-running an
overlapping range corrects bars rather than duplicating them.

Provider construction is now one place, `app/market/providers/factory.py`,
which also owns the sentence explaining what to configure. Splitting
`market_data_provider_or_reason` out of `market_data_provider` is not
decoration: an earlier round had to collapse a duplicated helper after
breaking *both* copies at once went unnoticed by the whole suite, and one
message written once cannot drift.

The credential stays process-level (`UPSTOX_DATA_ACCESS_TOKEN`) with **no
`BrokerAccount` row**, and that is load-bearing rather than tidy.
`resolve_broker` picks the most recent ACTIVE `BrokerAccount` for every
order a user places, and `_execution_mode_for` stamps anything that is not
a `MockBroker` as LIVE — so a row here would route that user's real orders
to Upstox as a side effect of wanting price history. A test asserts the
returned client has no `place_order`, `modify_order` or `cancel_order`
surface at all.

### The worker stops pretending

`app/workers/main.py` built `SimulatedFeed(candles_by_symbol={})` — an
empty dict — so `subscribe()` returned on its first step, `supervise`
logged "returned unexpectedly; restarting in 5s", and the process spent
its whole life restarting a generator structurally incapable of yielding.
Backing off to one attempt a minute, forever, while the log looked busy.

The bridge is now supervised only when the feed could actually produce
something. `_feed_can_emit` answers that statically for `SimulatedFeed`
alone — it replays a dict it was handed, so an empty one is a provable
dead end — and assumes anything else is live, because **a real feed's
silence is a fact about the market, not about the object**, and refusing
to start it would make the guard the cause of the outage it exists to
report.

Refusing is not the same as doing nothing: `scanner` and `autotrade` still
run against whatever the store holds, `market_data` correctly reads DOWN
on `GET /health` because nothing beats for it, and one `ERROR` line names
the gap and points at the backfill route.

### What is still missing, precisely

**There is no live streaming feed, and this round did not build one.**
`UpstoxMarketData` is REST only — historical candles and last-traded price,
no WebSocket — so a live feed would have to be a polling client that does
not exist. What this round delivers is *historical* ingestion: enough for
the scanner, the autonomous loop, backtests and out-of-sample validation
to run against real prices instead of nothing, and not enough to trade
intraday on live ticks.

Said plainly so the improvement is not mistaken for more than it is: the
system can now be fed, on demand, by an operator. It cannot yet feed
itself.

### Injection

The wiring — not the logic — was the uncovered half in three consecutive
earlier rounds, so both call sites were injected deliberately this time:

```
blank token no longer stripped                        -> 1 fail  (control)
the missing-provider reason stops naming the remedy   -> 2 fail
endpoint no longer guards a missing provider          -> 1 fail  (call site)
backfill endpoint drops its admin gate                -> 1 fail  (control)
endpoint stops calling backfill_candles at all        -> 1 fail  (call site)
the guard always says the feed can emit (original)    -> 3 fail
a real feed refused as if it were dead                -> 1 fail  (control)
```

All three controls were injection-tested alongside the proofs, and both
call-site injections were caught — the first round in four where that was
true on the first attempt.

## A 500 on `POST /auth/register` for anyone whose password is not ASCII (§68)

`RegisterRequest.password` carried `Field(min_length=8, max_length=72)`.
Pydantic's `max_length` counts **characters**. `hash_password` counts
**bytes**, because bcrypt reads only the first 72 of them and this codebase
refuses rather than truncates — two different long passwords must never
hash the same way.

The two units disagreed, and the gap is ordinary rather than exotic:

| password | characters | bytes | schema | hash |
|---|---|---|---|---|
| 72 ASCII letters | 72 | 72 | pass | fine |
| 20 emoji | 20 | 80 | pass | `PasswordTooLongError` |
| 30 CJK characters | 30 | 90 | pass | `PasswordTooLongError` |
| 40 accented Latin letters | 40 | 80 | pass | `PasswordTooLongError` |

Nothing in `app/` caught that exception. Measured through the real route
before the fix: **HTTP 500 with a traceback**, on an unauthenticated
endpoint, for a perfectly reasonable password. It was found by a probe for
custom exceptions that are raised but never caught.

The fix is a `field_validator` on `RegisterRequest.password` that measures
UTF-8 bytes, and a message that says so — "72 characters" would send the
user round the loop with another password that also fails, so the rejection
names the unit, how far over this one is, and that accented, CJK and emoji
characters cost more than one byte each. The character bound stays: it is a
necessary condition (no character encodes to less than one byte) and
rejects obvious oversize cheaply. `POST /auth/register` also catches
`PasswordTooLongError` and answers **422**, deliberately not the 409 a
duplicate email gets — a conflict with existing state and a malformed
request are different things to tell a client.

`POST /auth/login` was checked and is clean: `verify_password` returns
`False` for an over-long input rather than raising, so `LoginRequest`'s
unbounded password is not exposed. The PR was not widened for it.

### Two things injection found that review did not

**The fourth vacuous test this work has caught.** The first version of the
boundary control asserted that the schema and `hash_password` agreed, using
`MAX_PASSWORD_BYTES` on both sides. Moving the constant to 40 moved both
sides together and the entire suite stayed green. A control that compares a
value to itself tests nothing.

The second version was wrong in a more interesting way: it asked
`verify_password` where bcrypt stops reading. But `verify_password` returns
`False` above `MAX_PASSWORD_BYTES` before bcrypt ever sees the input, so it
only ever answers where *we* stop reading. It failed immediately against
bcrypt 4.2.0, which was measured directly and does truncate silently:
`checkpw(b"a"*73, hashpw(b"a"*72))` is `True`, and `hashpw` on 73 bytes does
not raise. The control now probes the bcrypt library itself — byte 72 must
still be read, byte 73 must not — and there is deliberately no
`assert MAX_PASSWORD_BYTES == 72`, which would pin the number without ever
checking it was the right one.

**And the wiring, again.** Removing the route's `except
PasswordTooLongError` left the whole auth suite green, because the schema
now rejects these passwords first and the handler's catch is never reached
down the ordinary path. That is defence in depth by definition — and an
untested defence is a claim, not a defence. A test now constructs the
request with `model_construct`, which is how a bypass actually happens
(it skips validators, as any internal caller building a `RegisterRequest`
directly would), and asserts the route answers 422 rather than letting the
raise become a 500. This is the fourth round in six where a call site,
not the logic, was the uncovered half.

## Option chains: three readers, no writer (§40, §52)

`option_snapshots` was read in four places and written in none.

`POST /options/execute` reads it for **all three** of its options-specific
gates — liquidity (volume, open interest, spread), premium deviation
against the real bid/ask mid, and quote staleness — and
`app/trading/portfolio_snapshots.py` reads it to mark option positions to
market. Nothing in `app/` had ever inserted a row. An earlier round found
this and did the honest half: it stopped the endpoint recording those
three checks as *passed* when nothing had looked, so the audit row says
"not assessed" instead of lying. Honest, and completely inert — an
operator could execute a multi-leg strategy against a contract nobody had
ever quoted, and the Greeks on every option position read 0.

This is the other half: `POST /admin/option-chain`, the options analogue
of `POST /admin/backfill`. Admin-gated and on demand rather than a
background loop, because which expiry to fetch is a judgement call and
provider chains are rate-limited. `UpstoxMarketData` gains
`get_option_chain`, with the response shape parsed by a pure
`parse_option_chain` so it is fixture-testable here and correctable from
one real call later — the same arrangement `parse_candles` has, and for
the same reason: this environment cannot reach Upstox's servers.

Deliberately append-only. Each run writes a new `option_chains` row with
fresh contracts and snapshots beneath it, because a snapshot *is* a
timestamped quote — yesterday's is not wrong, only old, and the staleness
gate downstream only means anything if the store keeps when each quote was
taken. Every quote in one fetch shares one timestamp, not a per-row clock,
so a strategy's legs cannot appear to differ in freshness because of the
order the writer happened to insert them.

Three things it refuses to do, each because the alternative would put
something false in the store:

- A chain that comes back without `underlying_spot_price` is refused, not
  stored with `spot_price` 0.0.
- A chain row with no `strike_price` is skipped, not stored at strike 0.0
  — which is a real strike, deep in the money, that the gates would read
  as one.
- A contract the provider quotes but this deployment has no `Instrument`
  row for is **reported**, not auto-created. Registering an instrument is
  `POST /instruments`' job; minting rows here would let a provider's
  spelling of a symbol quietly become this system's.

### The writer would have broken the readers

Both consumers resolved the instrument's `OptionContract` with
`.scalar_one_or_none()`. Nothing enforced one contract row per instrument,
and an ingestion run creates a fresh one under each new chain — so **the
second chain fetch of the day would have turned `POST /options/execute`
into a 500.** Measured directly against Postgres before any of this was
written:

    two contracts for one instrument -> MultipleResultsFound

The fix is one shared `app/options/snapshots.latest_option_snapshot`,
which joins contract to snapshot and takes the newest quote across every
fetch — the question both callers were actually asking. It replaces two
inlined copies of the same two queries, which is the deduplication an
earlier round learned to do the hard way: breaking *both* copies of a
duplicated helper at once had left the whole suite green.

So the feature and the trap ship together, and the trap is not
hypothetical — it is precisely what the feature's second run produces.

### What this does and does not deliver

Chains reach the store when an operator asks for them. That is enough for
the three execution gates to be real and for option Greeks to be marked to
market, and it is **not** a live quote stream: the freshness a strategy is
judged against is the freshness of the last fetch. As with equities, the
system can now be fed; it cannot yet feed itself.

## The backtest cost model was a bare `dict` (§47, §77-78)

`cost_model` on `POST /backtest` and `POST /backtest/validate` was typed
`dict`, with nothing between the request body and
`CostModel(**payload.cost_model)`. Two separate failures, both measured
through the real routes on one strategy over one set of candles.

**Four shapes returned HTTP 500 with a traceback.** `CostModel` is a slots
dataclass, so an unknown key raises `TypeError` at construction, and a
string or an explicit null raises `TypeError` later in the arithmetic.
`POST /backtest` did not guard the call at all; `POST /backtest/validate`
guarded it with `except ValueError`, which a `TypeError` walks straight
past.

| `cost_model` | before | after |
|---|---|---|
| `{"bogus": 1}` | 500 | 422 |
| `{"slippage_pct": "abc"}` | 500 | 422 |
| `{"slippage_pct": null}` | 500 | 422 |
| `{"brokerage_pct": [1, 2]}` | 500 | 422 |

**And the values that constructed fine were unbounded, which is the worse
half.** Every cost here exists to make a backtest *more* pessimistic. A
negative one is a subsidy paid on every fill:

```
slippage_pct   0.05  ->  net_profit      8,106.55
slippage_pct -50.0   ->  net_profit    224,464.33
```

Same strategy, same candles: a 27x edge that exists only in the cost
model, reported with exactly the same authority as a real result. A
backtest is the artifact someone decides to risk money on, and the cost
model is the one knob whose entire job is to stop it flattering the
strategy. `brokerage_pct: 500` was likewise accepted (-320,121) — absurd,
though at least in the safe direction.

`cost_model` is now a `CostModelRequest` with `extra="forbid"` and `ge=0`
on all six fields, plus an upper bound of 100% on the percentage ones
(above that a single round trip costs more than the position is worth,
which is a typo rather than a cost model). The bounds are **REASONED, NOT
CALIBRATED**. `ge=0` and not `gt=0` on purpose: a zero-cost run is the
documented default and a legitimate first look at a strategy, so what is
rejected is negative costs specifically, not small ones.

A control pins `CostModelRequest`'s field names against `CostModel`'s, so
a field added to one and not the other cannot silently fall back to its
default — a caller setting a cost and being ignored is worse than a 422.

### A negative result worth recording

`train_pct` and `validation_pct` are **fractions** (0–1) named `_pct` in a
codebase where every other `_pct` is 0–100, and they are request fields
with no `Field()` bounds. That looked like the same bug. It is not:
`split_periods` raises `ValueError` for anything outside (0, 1), and the
validate endpoint's `except ValueError` does catch that one. Measured:
`train_pct: 60` returns a clean **422** naming the constraint. The naming
is still inconsistent, and it is not worth an API break to fix.

## `POST /options/strategy` 500'd on its own default request (§37)

`BuildStrategyRequest.strategy_kwargs` is a `dict` that gets spread into
`build_strategy(...)`, and its default is `{}`. **Every one of the ten
builders requires at least one strike argument** — `long_call` needs a
`strike`, `iron_condor` needs four — so that default could not succeed for
any strategy. It raised `TypeError` (missing positional arguments), the
route caught only `(ValueError, KeyError)`, and the caller got HTTP 500
with a traceback.

Nothing anywhere told a caller what to send instead. `GET
/options/strategies` listed the ten names and no arguments, so the only
way to discover that `bull_call_spread` wants `long_strike` and
`short_strike` was to keep guessing against a 500.

Three more shapes did the same thing, all `TypeError` out of the `**kwargs`
spread: an unknown argument, and `quantity` or `chain` passed a second time
through `strategy_kwargs` ("multiple values for argument").

`build_strategy` now rejects all of them with a `ValueError` naming what
the strategy actually wants, read from the builder's own signature via
`required_arguments`, and `GET /options/strategies` reports the same thing
under `requires` so a caller can know before asking.

### And the sizes were unbounded

`quantity` and `lot_size` had no bounds. This endpoint answers a question
rather than placing a trade, so what that produced was a wrong number
rather than a bad fill — which is worse to leave than it sounds, because a
payoff summary is exactly what someone reads *before* choosing a strategy.
Measured on a 25000/25200 bull call spread:

| request | before | after |
|---|---|---|
| `quantity: -5` | 200, `net_premium: -17500` (a debit spread as a credit) | 422 |
| `lot_size: 0` | 200, every number `0.0` | 422 |
| `lot_size: -50` | 200, every number inverted | 422 |

### Two layers that masked each other

Worth recording because injection is the only reason it was found. The fix
has two: `build_strategy` rejecting bad arguments with a `ValueError`, and
the route catching any `TypeError` that still escapes. Every test went
through the endpoint — where **either layer alone produces a 422**.
Measured: removing `build_strategy`'s checks left the whole suite green,
because the route's guard caught the `TypeError`; removing the route's
guard left it green too, because the checks ran first.

Two layers that can stand in for each other are two layers nothing is
testing. There are now unit tests on `build_strategy` asserting it raises
`ValueError` rather than `TypeError` for every strategy, and an endpoint
test that monkeypatches a drifted builder so the route's guard is the only
thing that can answer. This is the same lesson as the password round's
`model_construct` test, in a new shape.

### One correction from this round's own work

The first attempt put the reserved-argument check (`quantity`, `lot_size`,
`chain` may not be passed twice) inside `build_strategy` — where it
**refused every correct call**, because that function's own caller passes
`quantity` and `lot_size` through the same `**kwargs` and by then they are
indistinguishable from smuggled ones. The check belongs at the API layer,
which still holds `strategy_kwargs` separately. `RESERVED_ARGUMENTS` is
exported for it, and `build_strategy` carries a comment saying why the
check is not there.

## The autonomous loop had no freshness gate (§54, §58)

`AutoTradeSupervisor` reads the newest stored candle for an instrument and
trades on it. Until now it did that **however old that candle was**.

`PaperTradingEngine` builds its `TradeRiskProposal` with
`market_data_age_seconds=0.0`, so the `market_data_fresh` check could not
fail on this path — and the `RiskEvent` row recorded it as a check that had
passed. Measured by driving the engine with a bar from January while the
clock read September:

```
proposal market_data_age_seconds=0.0   market_data_fresh=True
```

`POST /orders` computes a real age from Redis and enforces a limit. The
autonomous path — the one that trades unattended — had the gate in name
only. It is the same shape as the `strategy_allocation=0.0` an earlier
round had to fix on this very proposal, and `tests/paper/test_engine.py`
still carries that regression test.

Nothing about the stale case is exotic. `_last_candle_seen` is in-memory,
so a worker restart clears it and the very next pass acts on the newest
stored bar whatever its date — and candles only reach the store when an
operator runs `POST /admin/backfill`.

### Where the guard goes, and what it measures

At the supervisor, not in the engine. The supervisor is the only unattended
caller; the other one, `POST /paper/{id}/candle`, is an operator
deliberately handing over a bar, where freshness is not a property of
anything. The engine's `0.0` is now true by construction, and says so.

Lateness is measured **beyond the bar**, not from its stamp. A 15m bar
stamped at its open is not available until 15 minutes later, so the raw
difference would call every freshly closed bar 900s stale and refuse the
whole loop. The limit is `MAX_CANDLE_AGE_IN_BARS` (3) of the instrument's
own timeframe — **REASONED, NOT CALIBRATED**: a bar is expected within one
interval of its close and this loop polls every 60s, so one bar of slack
covers the ordinary case; at three, at least two bars are missing outright
and the feed has stopped rather than slipped.

Deliberately not `RiskLimits.market_data_max_staleness_seconds`. That is 10
seconds and describes a *tick*, which is what `POST /orders` measures; a
15m bar is 900 seconds old the instant it closes, so applying the tick
limit to bars would refuse every candle this loop has ever seen. A control
pins that relationship rather than the numbers.

### The fixtures were unrealistic in a new dimension

Turning the gate on failed 15 existing tests, all of which anchored their
candles at a fixed `datetime(2026, 1, 5)` — 256 days stale against the
clock they actually run under. The same call as the `volume=100.0` fixtures
when the liquidity gate went in: the gate is right and the anchor was
arbitrary. The series are now anchored to finish at roughly now.

One of those fixtures needed more than a re-anchor. `_seed(bar_count=N)`
took the *first* N bars, which is fine for `N=74` of 75 (the prefix still
ends a bar ago) and wrong for `N=2` (the two bars furthest in the past).
An instrument with almost no history is one that only just started being
tracked, so it now takes the newest N — which is both the passing fixture
and the more faithful one.

### What injection caught that the tests did not

Hard-coding the bar duration to `"1m"` in the supervisor's own call —
ignoring `strategy.timeframe` — left the whole suite green. A bar 15
minutes old and a bar a day old land on the same side of the limit
whichever duration is subtracted, so neither existing call-site test could
see it. There is now one sitting where the difference decides the answer: a
15m bar stamped 59 minutes ago is 44 minutes late and must trade, while
subtracting one minute instead of fifteen reads it as 58 minutes late and
refuses a perfectly healthy feed.

## A regression net for the "check that measured nothing" class (§57-58)

**A negative-result round.** No bug found; what it leaves behind is a net
for the failure this codebase has hit three times, plus a record of what is
now known clean so it does not get re-probed.

### The class

A field on a `RiskProposal` left at its dataclass default, so the check
reading it records a **pass** in the `RiskEvent` audit row while measuring
nothing:

| round | field | effect |
|---|---|---|
| 121 | `liquidity_acceptable` | no writer on the equity path |
| 134 | three options quote checks | recorded without ever being evaluated |
| 141 | `market_data_age_seconds=0.0` | `market_data_fresh` unfailable on the autonomous path |

Each was found by reading code. Nothing would have caught the next one: a
defaulted field produces a *passing* check and a green suite.

`POST /options/execute` now has a per-field behavioural net. For each
counter the engine reads, the stack is put in a state where that counter
**alone** must reject the order, and the test asserts the rejection names
that check. A field reading its default cannot produce that rejection.

Injection confirms all five wires: removing `trades_today`, `daily_pnl`,
`weekly_pnl`, `repeated_rejections` or `open_positions` from the proposal
each fails the net.

`open_positions` is worth naming separately. It is not a stack counter —
it is `len(...)` over the shared `PositionManager` — so the parametrised
poison could not reach it, and **defaulting it to 0 left the whole suite
green**. Injection found that hole in the net itself; it now has its own
proof that opens a real position.

### What the survey checked and found clean

Three probes, all negative, recorded so they are not repeated:

- **Equity vs options risk checks.** The equity engine runs 15 checks, the
  options engine 11. Three of the four differences are correct by design
  (`valid_stop_distance` — options bound loss through the payoff engine;
  `no_abnormal_price_jump` — `premium_matches_market` is the options
  analogue; `strategy_allocation_limit` — that path is manual, not
  strategy-driven). The fourth, `correlated_exposure_limit`, is a genuine
  gap and is recorded below rather than guessed at.
- **`current_exposure`.** Both paths compute it identically, from the same
  shared `PositionManager`.
- **Decimal/float across the DB boundary.** `Numeric` columns return
  `Decimal`, and mixing that with a float raises `TypeError`. A sweep of 55
  Numeric column names against arithmetic in `app/` produced 41 candidate
  sites, every one of which turned out to be a *dataclass* field whose name
  collides with a column (SMC candles, risk proposals, in-memory
  positions). The codebase converts at the ORM boundary consistently.

Two claims round 141 made without measuring were also verified rather than
left as assertions. The paper engine's `is_reducing=False` is right —
`risk_engine.evaluate` is called from exactly one place, the entry path,
and `_maybe_exit` does not go through it at all. Its
`entry_deviation_pct=0.0` is right too: that check exists to validate a
*client-supplied* price against a broker quote, and the paper engine
computes its own entry from the bar.

### The one real gap, deliberately not built

`POST /options/execute` has no correlated-exposure gate. An operator can
hold correlated equity longs and open a large bullish options position with
nothing netting the two, while the equity path has netted since §85-86 and
had its direction corrected later.

It is genuinely absent rather than inert — `OptionsRiskProposal` has no
such field — and the mechanics are reusable: `compute_correlated_exposure`,
`signed_notionals_excluding`, and `Instrument.underlying` all exist.

What does not exist is an agreed way to turn a multi-leg options strategy
into a **signed directional notional** for netting. `max_loss` (what the
exposure gate uses) is magnitude, not direction, and a bull call spread, a
short straddle and a long put have very different directional profiles.
Guessing would produce a plausible-looking but wrong risk number — the
exact failure the three rounds above spent their effort removing. The
netting model is a decision to make explicitly, not a detail to infer.

## A symbol with no instrument id had every candle silently discarded (§66)

`CandleWorker` builds a base candle per symbol per minute and, when the
symbol has an entry in `WORKER_INSTRUMENT_IDS`, stores it. When it does
not, there is no `Instrument` row to hang the candle on, so nothing is
stored — correctly. What it did not do was say so.

Measured before this change, feeding one closed candle for a symbol that
is in `WORKER_SYMBOLS` but absent from `WORKER_INSTRUMENT_IDS`:

    stored:       []
    published to: channel:chart:INFY:1m
    log records:  []

Nothing stored, nothing logged, nothing counted — while the worker kept
building a candle a minute and streaming every one of them. The streaming
is what makes the failure hard to see rather than merely quiet:
`/ws/chart` takes its `instrument_id` as a `str`, so a client subscribed
by symbol really does receive these, and every externally visible sign
says the pipeline works.

It does not. `ScannerWorker`, `AutoTradeSupervisor`, the backtest engine
and the replay engine all read the `candles` table. A symbol that never
reaches it is invisible to all four for as long as the deployment runs —
no signals, no autonomous entries, and a backtest over that symbol
returning an empty result rather than an error. The same family as the
market-data bridge that restarted forever without ever yielding a tick
(§66 above): a worker that looks busy and achieves nothing.

Two places now report it, because they answer different questions.

`app/workers/main.py` reports it **at startup**, listing every symbol in
`WORKER_SYMBOLS` with no id. That is the moment an operator is still
watching output and can fix the environment before the process settles
into its loop.

`CandleWorker` reports it **the first time a candle is actually dropped**,
which is the ground truth — the startup list is derived from configuration,
this is derived from a candle that existed and was thrown away. Once per
symbol, not once per candle: at one base candle a minute an undeduplicated
line would be 1,440 identical errors a day per misconfigured symbol, which
is how a real warning gets filtered out of a log. The Prometheus counter
`candles_dropped_unknown_instrument_total{symbol}` is **not** deduplicated
— it counts every drop, so the log says *what is wrong* and the metric
says *how much data has been lost since*.

The publish is deliberately kept for unmapped symbols. Dropping it would
have been a tempting tidy-up, and it would have broken the one thing on
this path that genuinely works.

One unrelated defect surfaced while testing the startup check through the
real `main()`: its shutdown (`task.cancel()` for each supervised worker,
then `gather`) sat after `await stop_event.wait()` with no `try`/`finally`,
so cancelling `main()` itself skipped the shutdown entirely and left every
supervised task running detached — asyncio's "Task was destroyed but it is
pending" is exactly that. Moved into a `finally`.

## The instrument master's numbers had no bounds (§9)

Round 106 restated the `instruments` string column widths inside
`InstrumentCreateRequest` after measuring four 500s, and stopped at the
strings. The numeric columns were never done. Measured against the live
schema:

| sent | answer |
|---|---|
| `lot_size=0` | 201 |
| `lot_size=-50` | 201 |
| `lot_size=2**31` | **500**, a raw asyncpg `DataError` (the column is `INTEGER`) |
| `tick_size=0`, `tick_size=-1` | 201 |
| `tick_size=1e30` | **500**, `NumericValueOutOfRange` on `NUMERIC(18,6)` |
| `strike=0`, `strike=-25000` | 201 |
| `strike=1e30`, `strike=inf` | **500**, `NumericValueOutOfRange` on `NUMERIC(18,4)` |

The 500s are the lesser half. What `instruments` is makes the accepted
values worse than a bad request normally is: the row is **global**, shared
by every user, writable by any authenticated one, and **there is no
endpoint that can edit or delete it** — the same property that made a
duplicate registration lock every holder of a symbol out of their own
position. A bad number here is a permanent, unrepairable property of that
symbol for everyone who trades it.

`lot_size` is the one that matters, because it is the scaling factor every
options order is sized by. Driving `POST /options/execute` against a
`lot_size=0` contract:

    execute -> 201
    payoff:    max_profit 0.0, max_loss 0.0, net_premium 0.0
    legs:      [('ACKNOWLEDGED', None)]
    positions: []

The caller is told a strategy executed. Nothing was opened, and the
`RiskEvent` row records an approval of a position that had no risk only
because it had no size — the same shape as the three rounds that removed
checks recording a pass without measuring anything.

A negative lot size is the other direction rather than a smaller version
of the same thing. `payoff` is `sign * (intrinsic - premium) * quantity *
lot_size`, so `lot_size=-50` silently turns a long call into a short one,
with the unbounded loss that implies. In this environment such an order
happened to be refused, but for the wrong reason: the exposure gate saw
the unbounded max loss the *inverted* position implies and rejected on
that. A smaller premium or a larger balance and it would have gone
through.

### Two layers, and they are not the same check

`InstrumentCreateRequest` now bounds `lot_size` (`ge=1`, `le=1_000_000`,
matching the identical bound `POST /options/strategy` already applies to
the *client-supplied* copy of this value), `tick_size` (`gt=0`) and
`strike` (`gt=0`). Every case in the table above is now a 422 naming the
field instead of a 201 or a traceback.

That decides what can be **written**. It cannot reach a row that already
exists — and every deployed database has some. So `POST /options/execute`
also refuses a contract whose stored `lot_size` is below 1, with a message
saying why and what to do. The registration bound and the execution guard
are tested separately, because a test that goes through the API passes
with either one alone.

`expiry` is deliberately still unbounded: a contract may be registered
with a date in the past. Nothing in `app/` reads `Instrument.expiry`, so
refusing one would be inventing a constraint nothing depends on — the same
call round 84 made when it required `strike` and `option_type` for
`market=OPTIONS` and left `expiry` alone. It should become a real
constraint when something finally prices against it.

## Two ways a strategy looks alive and does nothing (§91, §9)

### A `lookback` below 1 can never match

`evaluate_condition` asks `context.current_index - event.index <
condition.lookback` for `bos`, `mss`, `choch` and `liquidity_sweep`. On
the bar the event printed, that difference is 0 — so **1 is the smallest
value that can ever be true**, and it means "only on the event bar".

Measured through `StrategyEngine.evaluate` against a real BOS at index 7,
evaluated one bar later:

| lookback | result |
|---|---|
| 2 | `satisfied=['bos']` |
| 1 | `missing=['bos']` — correct, the event is a bar old |
| 0 | `missing=['bos']` |
| -5 | `missing=['bos']` |

and directly at the evaluator, on the event bar itself: `lookback=1` →
True, `lookback=0` → False, `lookback=-1000` → False.

`POST /strategies` answered **201** for the `lookback=0` version. Because
conditions AND implicitly (`evaluate_conditions`), one such condition
zeroes the whole strategy: it stores, lists, backtests and auto-trades
like any other and simply never produces a signal — and a validation
backtest reporting zero trades is indistinguishable from "this history
had no setups", so blueprint §77's graduation path cannot catch it at any
stage.

That is exactly the ruling `app/strategy/dsl.py` already makes three
times in its own words, for unfed condition types, for `premium_discount`
with no zone, and for unknown entry types: fail at authoring time rather
than look alive and do nothing. `Condition` now refuses `lookback < 1`.

No upper bound. A very large `lookback` means "this event never expires",
which is a defensible authoring choice — it is what every structure
condition did before the expiry window existed — and `lookback` lives
inside a JSON column, so there is no width to overflow. Only the end that
cannot match is refused.

Writing the injections found one more thing, in the fix itself: the
guard was originally `if self.lookback is not None and self.lookback < 1`,
and injecting a change into that `None` branch left the suite green. It
was unreachable — `_default_lookback_per_condition_type` is declared
above and pydantic runs `mode="after"` validators in declaration order,
so the field always holds an int by then. The dead half is gone, and the
ordering dependency is stated where it matters.

### A stored definition that no longer validates was a 500

`strategies.definition` is a JSON column written by whatever version of
the DSL was current when the row was saved, and re-validated against
whatever version is current when it is read. **Every validator this
codebase has added widened that gap.** A row written before the unfed-type
rule, before the `premium_discount` zone rule, or before the `lookback`
floor above, is stored data that no longer parses.

Measured by planting a definition that trips the unfed-type rule and
posting a backtest for it:

    POST /backtest -> pydantic_core.ValidationError, uncaught,
                      app/api/backtest.py:132

which the catch-all turns into a 500 with a traceback — for stored data
that is merely out of date, on the endpoint whose whole job is to tell
the author whether a strategy is any good.

`ScannerWorker` and `AutoTradeSupervisor` already get this right: both
wrap the same call per strategy in `try/except`, log "Strategy %s has an
invalid definition; skipping", and carry on with everyone else's
strategies. The six API call sites did not. They now all go through
`app/api/stored_strategies.py`, which answers **422** naming the
strategy, what is wrong with it, and that re-saving fixes it.

422 rather than 500 because the request is well-formed and the server is
healthy — the stored strategy is what is unusable. Not 200 with an empty
result, because a strategy that could not be loaded has not been
evaluated, and reporting "no signals" for it would be the same fabricated
claim several earlier rounds removed from the risk paths.

The two halves belong together: without the second, the first would turn
every already-stored `lookback=0` strategy from silently-dead into a 500.

## The rate limiter could lock an address out permanently (§69)

`check_rate_limit` was `INCR`, then — only when the count came back 1 —
`EXPIRE`: two round trips with an `await` between them. Anything that
interrupted that gap left the key with **no expiry at all**, and because
`EXPIRE` is only reached at count 1, no later call ever set one.

Measured against a live Redis by doing the `INCR` without the `EXPIRE`,
which is exactly what a crash, a cancellation or a dropped connection on
that second call leaves behind:

    ttl after the interrupted call:        -1        (no expiry)
    next six calls (limit 3, window 1s):   True True False False False False
    after the window has passed:           False
    ttl:                                   -1

False forever. The keys are `auth:login:<client ip>` and
`auth:register:<client ip>`, so that is one address permanently unable to
log in or sign up — there is no other login path, nothing expires to
recover it, and the only remedy is a human deleting the key in Redis by
hand. Redis now has persistence (§109), so the poisoned key survives a
restart too; that earlier fix makes this failure more durable, not less.

The gap is not exotic. It is one `await` between two round trips, and
`app/core/rate_limit.py` **already catches a Redis error there** and
answers 503 — which is precisely the interleaving that poisons the key.
The blip looks transient and leaves a permanent 429 behind it.

### The fix

One atomic Lua step:

```lua
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
```

The TTL check also **repairs** a key that has lost its expiry, rather than
only setting one on the first call, so anything already poisoned in a
running deployment heals on its next request instead of needing manual
intervention.

Deliberately not an unconditional `EXPIRE`. Refreshing the window on every
call would repair the TTL too — and would mean a key under sustained load
never expires, which is the same permanent denial wearing a different hat.
There is a control test for exactly that wrong fix.

### Why nothing caught it

`tests/conftest.py` sets `RATE_LIMIT_ENABLED=false` for the whole suite,
for a good reason: every test shares one client address, so a real limit
would trip on test volume rather than on abuse. The consequence is that no
test had ever driven the limiter through an endpoint at all. The two
call-site tests added here turn it back on for their own duration and put
the shared key back afterwards.

### Three probes that came back clean

Recorded so they are not repeated:

- **Stop-versus-target ordering when one bar contains both.** All three
  engines — backtest, paper and replay — check the stop first and the
  target in an `elif`, so the pessimistic outcome wins consistently. A
  strategy validated in a backtest is not flattered relative to what the
  paper engine would do.
- **Limiter state across processes.** It is Redis-backed, so several API
  workers share one bucket rather than each getting its own allowance.
- **Response fields named for one source and computed from another.** Spot
  checks on the admin health and portfolio surfaces found none;
  `active_broker_connections` really does count active broker accounts,
  and `total_realized_pnl` still comes from the persisted rows.

## A halt stopped the stop being enforced (§54, §57, §75)

`AutoTradeSupervisor.run_once` `continue`d past any halted user. For
entries that is exactly right. For exits it was the opposite of right,
because **there is no broker-side protective order behind an auto-traded
position**: `ensure_protective_stop` is called only from `POST /orders`,
so `PaperTradingEngine._maybe_exit`, run on the candles this loop feeds
it, *is* the stop.

Measured on the stop-loss fixture in
`tests/workers/test_auto_trade_worker.py`, halting the account after the
entry filled and before the bar that breaks the stop:

| | trades | open position |
|---|---|---|
| not halted | 1 | none |
| halted | **0** | **still open, stop 99.70, on a bar whose low was 90** |

The ruling this now follows is already made twice in this codebase, in
these words: reconciliation halts an account precisely when its positions
look wrong, which is "the worst moment to forbid closing them". `POST
/orders` and `POST /options/execute` both exempt a reducing order from the
halt for that reason, and `RiskEngine.evaluate`'s own comment draws the
same line — everything outside its entry-only block "applies to exits just
as much". This path had the exemption **missing rather than declined**.

A halted account is now still driven, with entries suppressed. The
suppression is precise rather than broad: `PaperTradingEngine.on_candle`
returns immediately after `_maybe_exit` whenever a position is open, so
feeding it with a position open can only ever close one — and with nothing
open the supervisor does not feed it at all, because that call *would*
evaluate an entry. A test drives a halted account through the whole setup
and asserts it opens nothing.

### Three gates that are left as they are, but no longer silent

The same abandonment happens when `auto_trading_enabled` is turned off,
when the AUTO_TRADE permission is revoked, and when the strategy that
opened the position is deactivated. Those are **not** changed here.
Whether the loop should keep honouring a stop it placed after the operator
switched the robot off has two defensible answers — `POST /orders`
requires the LIVE_TRADE permission for *every* order including a reducing
one, which argues for stopping; a stop that silently stops existing argues
for continuing — and that is a decision to make explicitly rather than
infer, the same call rounds 142 and 144 made on their own open questions.

What is not in question is that it must not be silent. Any open
auto-traded position that no pass fed a candle to is now reported once —
an error naming the position and its stop, and a `RECONCILIATION_REQUIRED`
notification telling the holder that nothing is evaluating it and there is
no broker-side order behind it. Once per position, not once per pass: this
loop runs every 60 seconds, and an operator who has to filter a warning
will not read it.

### A defect in the fix, found by its own control

The managed-position marker was first recorded where the *already open*
position is read. But the pass that **opens** a position sees none
beforehand, so every fresh entry reported itself as unmanaged on its own
pass — one spurious notification per trade, measured, which is precisely
how a real warning becomes noise. What makes a position managed is that a
candle reached its engine, so that is where the marker belongs now.

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
- **Not atomic**, and what is done about that. Once the strategy-level
  risk check approves, each leg is submitted as its own order through the
  same broker/persistence path `POST /orders` uses
  (`app.trading.persistence`, the resolved broker from
  `app.trading.broker_resolver`). Neither this codebase nor (unverified)
  Upstox/Dhan guarantee all-or-nothing multi-leg fills, so a later leg's
  rejection genuinely does not undo an earlier leg's fill. Every leg's own
  outcome is in the response, and since a partly executed batch leaves
  exposure nobody approved, `_remediate_partial_batch` unwinds and halts —
  see "A half-executed spread is a naked option" below.

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
  durably mirrored into Postgres, and both *positions* and *recent
  orders* are now rebuilt from that mirror when a stack is first built
  (see "A restarted process no longer starts flat" below). What remains
  is genuinely concurrent operation: two replicas that build their stacks
  at the same moment each rehydrate from the same snapshot and can still
  act on stale knowledge of each other's in-flight orders, and an order
  placed on replica A after replica B rehydrated is invisible to B until
  B rebuilds. Moving that state into Redis, like the trading halt, is
  what closes it — not attempted here. A single replica restarting is no
  longer affected.

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
