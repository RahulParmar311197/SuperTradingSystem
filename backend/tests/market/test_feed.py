import pytest

from app.market.feed import SimulatedFeed
from tests.smc.conftest import make_candles
from tests.smc.test_swings import OHLC


@pytest.mark.asyncio
async def test_simulated_feed_replays_candles_as_ticks():
    candles = make_candles(OHLC)
    feed = SimulatedFeed({"NIFTY": candles}, exchange="NSE", market="EQUITY")

    ticks = [t async for t in feed.subscribe(["NIFTY"])]

    assert len(ticks) == len(candles)
    assert ticks[0].symbol == "NIFTY"
    assert ticks[-1].ltp == candles[-1].close
