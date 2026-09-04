from datetime import datetime, timezone

from app.core.redis import get_latest_price
from app.market.feed import SimulatedFeed
from app.market.normalization import StandardTick
from app.workers.market_data_worker import MarketDataWorker


async def test_process_tick_updates_redis_price_cache(require_infra):
    feed = SimulatedFeed({}, exchange="NSE", market="EQUITY")
    worker = MarketDataWorker(feed, symbols=["TESTSYM"])

    tick = StandardTick(
        symbol="TESTSYM", exchange="NSE", market="EQUITY", timestamp=datetime.now(timezone.utc), ltp=12345.6
    )
    await worker.process_tick(tick)

    assert await get_latest_price("TESTSYM") == 12345.6
