import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.auth.security import hash_password
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
