import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.database.models.instruments import Instrument, MarketType
from app.database.models.trading import ExecutionMode, Position
from app.database.models.users import User
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.risk.portfolio import compute_correlated_exposure, compute_portfolio_exposure
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio


async def _make_user(db) -> uuid.UUID:
    user = User(
        email=f"portfolio-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        name="Portfolio Test",
        trading_permissions=[],
    )
    db.add(user)
    await db.flush()
    return user.id


async def test_compute_portfolio_exposure_sums_open_positions_by_market(require_infra):
    async with async_session_factory() as db:
        user_id = await _make_user(db)
        equity = Instrument(symbol=f"EQ{uuid.uuid4().hex[:6]}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        future = Instrument(symbol=f"FUT{uuid.uuid4().hex[:6]}", exchange="NSE", market=MarketType.FUTURES, instrument_type="FUT")
        db.add_all([equity, future])
        await db.flush()

        db.add_all(
            [
                Position(
                    user_id=user_id, instrument_id=equity.id, execution_mode=ExecutionMode.LIVE,
                    quantity=10, average_price=100, is_open=True,
                ),
                Position(
                    user_id=user_id, instrument_id=future.id, execution_mode=ExecutionMode.LIVE,
                    quantity=5, average_price=200, is_open=True,
                ),
                Position(
                    user_id=user_id, instrument_id=equity.id, execution_mode=ExecutionMode.LIVE,
                    quantity=0, average_price=0, is_open=False,  # closed - must not count
                ),
            ]
        )
        await db.commit()

        try:
            exposure = await compute_portfolio_exposure(db, user_id)
            assert exposure.total_exposure == pytest.approx(10 * 100 + 5 * 200)
            assert exposure.exposure_by_market["EQUITY"] == pytest.approx(1000.0)
            assert exposure.exposure_by_market["FUTURES"] == pytest.approx(1000.0)
        finally:
            await db.execute(delete(Position).where(Position.user_id == user_id))
            await db.execute(delete(Instrument).where(Instrument.id.in_([equity.id, future.id])))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_compute_correlated_exposure_uses_real_candle_history(require_infra):
    async with async_session_factory() as db:
        base = f"CORR{uuid.uuid4().hex[:6].upper()}"
        target = Instrument(symbol=f"{base}A", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        correlated = Instrument(symbol=f"{base}B", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        uncorrelated = Instrument(symbol=f"{base}C", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add_all([target, correlated, uncorrelated])
        await db.flush()

        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        target_closes = [100, 102, 101, 105, 103, 108, 106, 110]
        correlated_closes = [c * 2 for c in target_closes]  # moves in lockstep
        uncorrelated_closes = [50, 40, 60, 30, 70, 20, 80, 10]  # unrelated pattern

        for instrument, closes in ((target, target_closes), (correlated, correlated_closes), (uncorrelated, uncorrelated_closes)):
            candles = [Candle(start + timedelta(minutes=i), c, c, c, c, 100) for i, c in enumerate(closes)]
            await upsert_candles(db, instrument.id, "15m", candles)

        try:
            exposure = await compute_correlated_exposure(
                db,
                target_symbol=target.symbol,
                target_notional=0.0,
                open_position_notionals={correlated.symbol: 1000.0, uncorrelated.symbol: 1000.0},
                threshold=0.9,
            )
            # Only the perfectly-correlated position's notional should count.
            assert exposure == pytest.approx(1000.0)
        finally:
            from app.database.models.market import Candle as CandleRow

            for instrument in (target, correlated, uncorrelated):
                await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument.id))
            await db.execute(delete(Instrument).where(Instrument.id.in_([target.id, correlated.id, uncorrelated.id])))
            await db.commit()
