import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.database.models.strategy import Setup as SetupRow
from app.database.models.strategy import Signal as SignalRow
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.users import User
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.smc.types import Candle
from app.workers.scanner_worker import ScannerWorker

# Reuse the bullish sweep+FVG dataset that's already proven to produce a
# matching signal in tests/strategy/test_engine.py.
SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
]


async def test_scanner_finds_a_match_and_persists_a_signal(db_instrument):
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [
        Candle(start + timedelta(minutes=i), o, h, l, c, 100)
        for i, (o, h, l, c) in enumerate(SETUP)
    ]

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"scanner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Scanner Test",
        )
        db.add(user)
        await db.flush()

        strategy = StrategyRow(
            user_id=user.id,
            name="Bullish FVG retest",
            definition={
                "name": "Bullish FVG retest",
                "market": "TESTSYM",
                "timeframe": "15m",
                "direction": "bullish",
                "conditions": [{"type": "fvg", "direction": "bullish"}],
                "entry": {"type": "fvg_retest"},
                "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
            },
            is_active=True,
        )
        db.add(strategy)
        await db.commit()
        strategy_id = strategy.id
        user_id = user.id

        await upsert_candles(db, db_instrument.id, "15m", candles)

    try:
        worker = ScannerWorker(timeframe="15m")
        results = await worker.run_once()

        matched = [r for r in results if r["instrument_id"] == str(db_instrument.id)]
        assert matched, "expected the scanner to report a result for this instrument"
        assert matched[0]["matched"] is True

        async with async_session_factory() as db:
            # Scope to this test's own strategy: the shared dev DB may hold
            # other active strategies that also match this instrument.
            signals = (
                await db.execute(
                    select(SignalRow).where(
                        SignalRow.instrument_id == db_instrument.id, SignalRow.strategy_id == strategy_id
                    )
                )
            ).scalars().all()
        assert len(signals) == 1
        assert signals[0].direction.value == "LONG"
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(SignalRow).where(SignalRow.strategy_id == strategy_id))
            await db.execute(delete(StrategyRow).where(StrategyRow.id == strategy_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_scanner_dedups_signals_across_passes(db_instrument):
    # Regression test: unlike `_persist_new_setups` (see the dedup test
    # below, for the sibling `setups` table), `_evaluate` wrote a brand
    # new `Signal` row and re-published to /ws/signals on *every* scan
    # pass a strategy continued to match -- with no dedup at all. In
    # production this worker re-scans the same closed 15m candle set
    # ~15 times per candle (interval_seconds=60 vs timeframe=15m,
    # app/workers/main.py), and a condition like "an unmitigated FVG
    # exists" stays true for as long as that gap remains unfilled --
    # often many passes -- so one genuine setup flooded `GET /signals`
    # and spammed connected clients with duplicate alerts every minute.
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(SETUP)]

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"scannerdedup-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Scanner Dedup Test",
        )
        db.add(user)
        await db.flush()

        strategy = StrategyRow(
            user_id=user.id,
            name="Bullish FVG retest",
            definition={
                "name": "Bullish FVG retest",
                "market": "TESTSYM",
                "timeframe": "15m",
                "direction": "bullish",
                "conditions": [{"type": "fvg", "direction": "bullish"}],
                "entry": {"type": "fvg_retest"},
                "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
            },
            is_active=True,
        )
        db.add(strategy)
        await db.commit()
        strategy_id = strategy.id
        user_id = user.id

        await upsert_candles(db, db_instrument.id, "15m", candles)

    try:
        worker = ScannerWorker(timeframe="15m")
        await worker.run_once()

        async with async_session_factory() as db:
            signals = (
                await db.execute(
                    select(SignalRow).where(SignalRow.instrument_id == db_instrument.id, SignalRow.strategy_id == strategy_id)
                )
            ).scalars().all()
        assert len(signals) == 1
        first_signal_id = signals[0].id

        # A second pass over the same, unchanged candle history -- the
        # gap is still unmitigated, so the strategy matches again -- must
        # not write a second row for the same still-active setup.
        await worker.run_once()
        async with async_session_factory() as db:
            signals_after = (
                await db.execute(
                    select(SignalRow).where(SignalRow.instrument_id == db_instrument.id, SignalRow.strategy_id == strategy_id)
                )
            ).scalars().all()
        assert len(signals_after) == 1
        assert signals_after[0].id == first_signal_id
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(SignalRow).where(SignalRow.strategy_id == strategy_id))
            await db.execute(delete(StrategyRow).where(StrategyRow.id == strategy_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_scanner_persists_setups_and_dedups_across_passes(db_instrument):
    # Regression test for the `setups` table (blueprint §9): raw SMC
    # detections behind a scan (structure breaks, FVGs, order blocks) had
    # zero writers anywhere before this -- they're independent of whether
    # any strategy actually matched, so this needs no active strategy at
    # all to exercise.
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(SETUP)]

    async with async_session_factory() as db:
        await upsert_candles(db, db_instrument.id, "15m", candles)

    worker = ScannerWorker(timeframe="15m")
    await worker.run_once()

    async with async_session_factory() as db:
        setups = (await db.execute(select(SetupRow).where(SetupRow.instrument_id == db_instrument.id))).scalars().all()
    assert setups, "expected the scan pass to journal at least one raw SMC detection"
    assert any(s.setup_type == "fair_value_gap" for s in setups)
    first_pass_count = len(setups)

    # A second pass over the same, unchanged candle history must not
    # duplicate rows for detections already journaled.
    await worker.run_once()
    async with async_session_factory() as db:
        setups_after = (await db.execute(select(SetupRow).where(SetupRow.instrument_id == db_instrument.id))).scalars().all()
    assert len(setups_after) == first_pass_count
