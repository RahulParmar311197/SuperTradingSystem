# AI Trading Platform — Project Status

Last updated: 2026-09-04

## Current branch

`main`

## Current stage

**Stage 1 — Backend foundation, market data normalization, SMC swing/structure engine**

Per the blueprint's "Project Development Order" (section 121), this repository
is being built in this order:

```
1. Backend foundation      <- in progress
2. Database                <- in progress (core tables)
3. Market data              <- in progress (normalization + validation)
4. Chart API
5. SMC engine               <- in progress (swings, BOS)
6. ICT engine
7. Strategy engine
8. Replay
9. Backtest
10. Paper trading
11. AI
12. Options engine
13. Risk engine
14. Dhan integration
15. Upstox integration
16. Android
17. Notifications
18. Monitoring
19. Limited live trading
20. Autonomous trading
```

Live/autonomous trading is out of scope until the above stages are built and
validated in order (blueprint section 132, "Production Readiness Checklist").

## Implemented

- FastAPI backend skeleton with `/health` and `/ready` endpoints.
- PostgreSQL schema for core tables: `users`, `instruments`, `candles`
  (blueprint sections 9, 12, 13).
- SQLAlchemy models and a DB session dependency.
- Market data normalization: raw candle payload -> canonical `MarketEvent`,
  with OHLC sanity validation (rejects non-finite values, high < low, etc.).
- Deterministic SMC swing detection (configurable `swing_length`, classifies
  `HH` / `HL` / `LH` / `LL`) with look-ahead prevention — a swing is only
  confirmed once enough future candles exist to validate it, and detection
  never uses data past the point being evaluated in replay-style processing.
- Deterministic BOS (Break of Structure) detection built on confirmed swings
  only.
- Docker Compose dev environment: `backend`, `postgres`, `redis`.
- Unit tests for normalization, swing detection, and BOS detection.

## Not yet implemented

- CHoCH / MSS, liquidity engine, FVG, order blocks, premium/discount.
- Chart / instrument / candle REST APIs beyond health checks.
- Strategy engine, Strategy DSL.
- Replay engine, backtest engine.
- AI integration (advisory only, per Golden Rule — never a final authority).
- Options engine, Greeks, payoff calculations.
- Risk engine, kill switch.
- Dhan / Upstox broker adapters.
- Paper trading, live trading, autonomous trading.
- Android application.

## Safety status

No execution path exists yet — this stage is data/analysis only. Nothing in
this repository can place, modify, or cancel a broker order.
