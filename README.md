# SuperTradingSystem — AI Trading Platform

Android-first, multi-market AI trading platform built from
[`AI_TRADING_PLATFORM_BLUEPRINT.md`](AI_TRADING_PLATFORM_BLUEPRINT.md).

## Development philosophy

```
Analyze -> Replay -> Backtest -> Paper Trade -> Controlled Live Trade
```

The AI is never the final execution authority. Every live order must pass
through deterministic strategy validation, the risk engine, and the
execution engine before it reaches a broker. See blueprint section 131
("Golden Rule"): `AI != Broker`, `AI != Risk Manager`, `AI != Final Authority`.

## Project status

See [`PROJECT_STATUS.md`](PROJECT_STATUS.md) for the current stage and
implementation checklist. Update it after any meaningful change.

## Stack

- Backend: Python, FastAPI, Pydantic, SQLAlchemy
- Data: PostgreSQL, Redis
- Quant: NumPy, Pandas
- Infrastructure: Docker Compose
- Android (future): Kotlin, Jetpack Compose, MVVM, Clean Architecture

## Repository layout

```
backend/        FastAPI service, market data, SMC/ICT engine, database models
database/       SQL schema / init scripts
tests/          Backend unit tests
docs/           Architecture notes
.env.example    Configuration template (never commit real secrets)
docker-compose.yml
```

## Running locally

```bash
cp .env.example .env
docker compose up --build
```

API docs: `http://localhost:8000/docs`

## Security

Never commit real API keys, broker credentials, JWT secrets, or production
environment files. Use `.env.example` as the template only.
