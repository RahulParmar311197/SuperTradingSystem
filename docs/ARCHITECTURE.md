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
