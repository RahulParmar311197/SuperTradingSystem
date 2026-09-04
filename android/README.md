# Android App — Scaffold

This is a **structural scaffold**, not a working app yet. It lays out the
package structure from the blueprint (§6) — `core/`, `data/`, `domain/`,
`features/*` — with placeholder files so feature work can start without
first deciding on project layout.

It has **not** been built or run in this environment (no Android
SDK/emulator available here). Before writing real features:

1. Open in Android Studio, let it generate the Gradle wrapper jar
   (`gradle wrapper --gradle-version 8.7` or via Android Studio's sync).
2. Confirm the module builds with the Compose/Hilt/Retrofit versions
   pinned in `app/build.gradle.kts` — bump them to current stable releases
   first, since they'll be stale by the time this is picked up.
3. Point `core/network` at the backend's base URL (`backend/app/main.py`,
   default `http://localhost:8000`).

## Layout

Matches blueprint §6:

```
core/        network, database, security, ui, websocket
data/        repositories, models, api (Retrofit interfaces)
domain/      models, usecases
features/*   one package per screen area (auth, dashboard, markets, chart,
             scanner, ai, strategy, options, replay, backtest, paper,
             portfolio, orders, settings)
```

Architecture: `UI -> ViewModel -> UseCase -> Repository -> API/Database`.
