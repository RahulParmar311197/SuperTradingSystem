# SuperTradingSystem

Android-first, multi-market AI trading platform (SMC/ICT analysis, AI-assisted
strategy building, replay/backtest/paper trading, Dhan/Upstox execution under
a deterministic risk engine). Full product spec: [`AI_TRADING_PLATFORM_BLUEPRINT.md`](AI_TRADING_PLATFORM_BLUEPRINT.md).

Current implementation status: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Layout

```
backend/          FastAPI backend — market data, SMC/ICT, strategy, risk,
                   execution, options, replay, backtest, paper trading, AI
android/           Android app scaffold (package structure only)
infrastructure/    nginx/monitoring config examples (not yet wired in)
scripts/           dev helper scripts
docs/              architecture/status notes
docker-compose.yml api + worker + postgres + redis for local dev
```

## Quickstart (backend)

```bash
cd backend
pip install -r requirements.txt
pytest tests
uvicorn app.main:app --reload
```

Or `./scripts/dev_up.sh` from the repo root to run everything via Docker
Compose (copies `.env.example` to `.env` on first run).
