# Android App — Scaffold

This is a **structural scaffold**, not a working app yet, and it is much
thinner than the blueprint's layout: **three** of blueprint §6's package
directories exist, not all of them. The section below separates what is
on disk from what §6 asks for, because an earlier version of this file
presented the full §6 tree under "Matches blueprint §6" and that was not
true of the checkout.

It has **not** been built or run in this environment (no Android
SDK/emulator available here), so nothing here is known to compile.

## What is actually here

Twelve files. Two of them are real working code; the rest are stubs or
build configuration.

```
app/src/main/kotlin/com/aitrading/app/
  core/network/ApiClient.kt            Retrofit client, bearer-token
                                       interceptor, base URL 10.0.2.2:8000
                                       (the emulator's alias for the host)
  core/security/TokenStore.kt          access/refresh tokens in
                                       EncryptedSharedPreferences, Android
                                       Keystore-backed
  features/dashboard/DashboardScreen.kt  placeholder Composable; it renders
                                       "Backend not connected"
  MainActivity.kt                      stub
  AiTradingApplication.kt              stub
```

Plus `AndroidManifest.xml`, `res/values/styles.xml`, three Gradle files
and `gradle.properties`. There is no Gradle wrapper jar (see step 1
below).

`TokenStore` is worth reading before adding anything that touches
credentials: it holds a **session token only**. Broker credentials are
never stored on-device (blueprint §70) — they live encrypted server-side,
and the app only ever carries a backend JWT.

## What blueprint §6 asks for, and is NOT here yet

```
core/        database, ui, websocket          (network, security exist)
data/        repositories, models, api        (none exist)
domain/      models, usecases                 (none exist)
features/*   auth, markets, chart, scanner, ai, strategy, options,
             replay, backtest, paper, portfolio, orders, settings
                                              (only dashboard exists)
```

Architecture §6 specifies: `UI -> ViewModel -> UseCase -> Repository ->
API/Database`. Nothing on disk implements the ViewModel, UseCase or
Repository layers yet — `DashboardScreen` has no ViewModel behind it.

## Before writing real features

1. Open in Android Studio, let it generate the Gradle wrapper jar
   (`gradle wrapper --gradle-version 8.7` or via Android Studio's sync).
2. Confirm the module builds with the Compose/Hilt/Retrofit versions
   pinned in `app/build.gradle.kts` — bump them to current stable releases
   first, since they'll be stale by the time this is picked up.
3. Point `core/network` at the backend's base URL (`backend/app/main.py`,
   default `http://localhost:8000`; the emulator reaches the host as
   `10.0.2.2`, which is what `ApiClient` already uses).

## Scope note

The backend exposes 89 HTTP routes and this app consumes effectively none
of them. Every round of work so far has been backend-only, so treat the
gap between this README's two lists as the actual remaining client work
rather than as an oversight.
