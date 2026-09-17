"""The equity liquidity gate (blueprint §57).

An earlier round found `TradeRiskProposal.liquidity_acceptable` defaulting
to `True` with no writer, so every equity order's `RiskEvent` recorded a
passed liquidity check that nothing had performed. That round made the
field `bool | None` and skipped it when unset -- an honest audit row, but
still no gate. This is the gate.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.database.models.instruments import Instrument, MarketType
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.risk.liquidity import (
    assess_equity_liquidity,
    assess_participation,
    average_bar_volume,
)
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio


# --- the rule itself -------------------------------------------------------


async def test_an_order_that_would_be_most_of_a_bar_is_rejected():
    """Behavioural proof. 500 shares against a bar that trades 1,000 is
    50% participation, five times a 10% cap."""
    assert assess_participation(500.0, average_bar_volume=1000.0, max_participation_pct=10.0) is False


async def test_an_order_well_inside_the_cap_passes():
    """Behavioural proof of the other branch -- a gate that only ever says
    no is not a gate, it is an outage."""
    assert assess_participation(50.0, average_bar_volume=1000.0, max_participation_pct=10.0) is True


async def test_the_cap_is_a_rate_and_not_a_floor():
    """Control, and the definition of the whole design.

    An absolute minimum-volume floor gets both ends wrong, and this pins
    both: a small order in a thin instrument fills fine and must pass,
    while a large order in a liquid one moves the price and must not. A
    floor would invert both verdicts, which is why the options module's
    `min_volume` shape was deliberately not reused here.
    """
    thin_but_small = assess_participation(5.0, average_bar_volume=100.0, max_participation_pct=10.0)
    liquid_but_huge = assess_participation(500_000.0, average_bar_volume=1_000_000.0, max_participation_pct=10.0)
    assert thin_but_small is True, "a tiny order in a thin name fills fine"
    assert liquid_but_huge is False, "half a bar is half a bar, however liquid the name"


async def test_the_boundary_is_inclusive():
    """Exactly at the cap passes: the limit is the largest allowed share,
    not the smallest forbidden one, matching every other `<=` limit in
    `RiskEngine.evaluate`."""
    assert assess_participation(100.0, average_bar_volume=1000.0, max_participation_pct=10.0) is True
    assert assess_participation(100.01, average_bar_volume=1000.0, max_participation_pct=10.0) is False


# --- not assessed is not the same as assessed and fine ---------------------


async def test_no_volume_history_is_not_assessed_rather_than_passed():
    """Behavioural proof, and the distinction this whole line of work
    exists to preserve. `None` in means `None` out: the check is skipped,
    never recorded as passed. Returning `True` here would recreate the
    fabricated audit row exactly."""
    assert assess_participation(500.0, average_bar_volume=None, max_participation_pct=10.0) is None


async def test_a_window_in_which_nothing_traded_rejects():
    """Control on the other side of that distinction. Zero is not missing
    data -- it is data, saying the instrument traded nothing across the
    whole window. An order into it cannot fill at any price we could
    reason about, and a participation rate against zero is undefined
    rather than small."""
    assert assess_participation(1.0, average_bar_volume=0.0, max_participation_pct=10.0) is False


async def test_no_database_session_is_not_assessed():
    """`PaperTradingEngine` can run without a session. No session means no
    assessment, never a fabricated pass."""
    assert await assess_equity_liquidity(None, symbol="ANY", quantity=1.0, max_participation_pct=10.0) is None


# --- against real stored candles -------------------------------------------


async def test_average_bar_volume_reads_the_recent_window_not_all_history(require_infra):
    """Behavioural proof. An instrument that was liquid a month ago and is
    thin today must be judged on today: averaging all history would let a
    dead name keep trading on its reputation.
    """
    async with async_session_factory() as db:
        symbol = f"LIQ{uuid.uuid4().hex[:6].upper()}"
        instrument = Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id

        start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
        # 30 busy bars, then 20 thin ones. The window is 20.
        volumes = [1_000_000.0] * 30 + [100.0] * 20
        await upsert_candles(
            db,
            instrument_id,
            "15m",
            [Candle(start + timedelta(minutes=15 * i), 100.0, 101.0, 99.0, 100.0, v)
             for i, v in enumerate(volumes)],
        )
        await db.commit()

        try:
            average = await average_bar_volume(db, symbol, timeframe="15m", window=20)
            assert average == pytest.approx(100.0), (
                "the busy past must not be averaged into today's verdict"
            )
            # ... and the gate follows from it: an order sized for the old
            # market is refused in the new one.
            assert await assess_equity_liquidity(
                db, symbol=symbol, quantity=1000.0, max_participation_pct=10.0
            ) is False
        finally:
            from app.database.models.market import Candle as CandleRow

            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
            await db.commit()


async def test_a_symbol_with_no_instrument_row_is_not_assessed(require_infra):
    """Control. An unregistered symbol is missing data, not zero volume,
    so it must skip the check rather than reject -- the same call that
    would reject on a real zero returns `None` here."""
    async with async_session_factory() as db:
        assert await assess_equity_liquidity(
            db, symbol=f"NOSUCH{uuid.uuid4().hex[:6].upper()}", quantity=1.0, max_participation_pct=10.0
        ) is None


async def test_a_registered_symbol_with_no_candles_is_not_assessed(require_infra):
    """Control. Registered but never quoted is still missing data."""
    async with async_session_factory() as db:
        symbol = f"EMPTY{uuid.uuid4().hex[:6].upper()}"
        instrument = Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.commit()
        instrument_id = instrument.id
        try:
            assert await average_bar_volume(db, symbol) is None
            assert await assess_equity_liquidity(
                db, symbol=symbol, quantity=1.0, max_participation_pct=10.0
            ) is None
        finally:
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
            await db.commit()


async def test_a_database_failure_downgrades_to_not_assessed(monkeypatch):
    """Control. A liquidity assessment is a refinement on the exposure
    checks, not a precondition for them, so a database hiccup must not
    take down the order path -- and must not pass the check either.
    """
    import app.risk.liquidity as liquidity_module

    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(liquidity_module, "average_bar_volume", boom)

    class _Session:
        pass

    assert await assess_equity_liquidity(
        _Session(), symbol="ANY", quantity=1.0, max_participation_pct=10.0
    ) is None
