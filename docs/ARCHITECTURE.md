# Implementation Status

This tracks what exists in code against the stages in
`AI_TRADING_PLATFORM_BLUEPRINT.md` §134 ("Project Status Definition").

| Stage | Blueprint area | Status |
|---|---|---|
| 0 | Architecture | Backend scaffolded (`backend/app/*`), repo layout matches §129 |
| 1 | Market data | `app/market`: normalization, timeframe/candle aggregation, simulated feed. No live broker feed yet (needs Dhan/Upstox adapters below). |
| 2 | SMC/ICT | `app/smc`, `app/ict`: swings, BOS/CHoCH/MSS, liquidity + sweeps, FVG, order blocks, premium/discount, kill zones, opening ranges. Fully unit-tested, look-ahead safe by construction. |
| 3 | Replay | `app/replay`: clock + manual BUY/SELL/SL/TP/CLOSE, statistics. Look-ahead safety proven by test (`tests/replay/test_engine.py`). |
| 4 | Backtesting | `app/backtest`: event loop reusing the same SMC/ICT/Strategy code as replay, configurable cost model, full metrics report, train/validation/test split helper. |
| 5 | AI | `app/ai`: structured context builder, Strategy-DSL JSON validation, AI trade-proposal validation against deterministic results, deterministic trade explanations. No LLM provider wired — `AIClient` is pluggable, ships with a `NullAIClient` (§110 "no AI -> no trade"). |
| 6 | Options | `app/options`: Black-Scholes Greeks, multi-leg payoff engine (max profit/loss/breakevens), liquidity filter, named strategy builders (spreads, condor, butterfly, straddle, strangle). |
| 7 | Paper trading | `app/paper`: strategy -> risk -> broker -> position manager -> portfolio, built on the same order/broker stack as live trading. |
| 8 | Dhan/Upstox | `app/brokers/dhan`, `app/brokers/upstox`: adapter skeletons implementing the `Broker` interface. HTTP calls are TODO — see the docstring in each `adapter.py` for the rollout checklist. Do not enable live trading against these until implemented and tested against the brokers' current official docs. |
| 9 | Controlled live trading | `app/api/orders.py` exercises the full risk-gate -> execution -> position flow, but currently only against `MockBroker` (no live broker wired yet). |
| 10 | Autonomous trading | Not started. The building blocks (scanner, strategy engine, risk engine, execution engine) exist; the always-on watch/scan/detect/analyze/validate loop described in §128 still needs a background worker (§66) to drive it. |

## What's deliberately not implemented

- **Background workers** (§66: `MarketDataWorker`, `ScannerWorker`, etc.) — the
  engines exist as libraries; nothing runs them on a schedule yet.
- **WebSocket channels** (§64) — REST only so far.
- **Android app** — `android/` is a package-structure scaffold (§6), not a
  working app; it hasn't been built or run (no Android SDK in this
  environment).
- **Real AI provider** — `app/ai/client.py` is an interface; plug in a real
  `AIClient` implementation and set `AI_PROVIDER`/`AI_API_KEY` to enable it.
- **Real Dhan/Upstox connectivity** — adapters are structurally complete
  but every HTTP call is a `NotImplementedError` TODO, per the blueprint's
  instruction to always implement against each broker's *current* official
  API docs rather than guessed endpoints.

## Running the backend

```bash
cd backend
pip install -r requirements.txt
pytest tests            # 70+ unit/integration tests, no external services needed
uvicorn app.main:app --reload
```

Or via Docker Compose from the repo root: `./scripts/dev_up.sh`.
