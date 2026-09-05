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
