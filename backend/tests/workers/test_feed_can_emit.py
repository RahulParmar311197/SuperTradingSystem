"""A bridge that cannot produce a tick must not be supervised.

`app/workers/main.py` built `SimulatedFeed(candles_by_symbol={})` -- an
empty dict -- so `subscribe()` returned on its first step, `supervise`
logged "returned unexpectedly; restarting in 5s", and the worker process
spent its entire life restarting a generator structurally incapable of
yielding. Backed off to one attempt a minute, forever, while an operator
reading the log saw something that looked busy.
"""

import pytest

from app.market.feed import SimulatedFeed
from app.smc.types import Candle
from app.workers.main import _feed_can_emit

pytestmark = pytest.mark.asyncio

_A_CANDLE = Candle.__new__(Candle)


def _simulated(mapping) -> SimulatedFeed:
    return SimulatedFeed(candles_by_symbol=mapping, exchange="NSE", market="EQUITY")


async def test_an_empty_simulated_feed_cannot_emit():
    """Behavioural proof, and the state every deployment was actually in."""
    assert _feed_can_emit(_simulated({}), ["NIFTY"]) is False


async def test_a_simulated_feed_with_no_candles_for_these_symbols_cannot_emit():
    """Behavioural proof of the subtler half: the dict is non-empty, but
    not for anything subscribed. `subscribe` iterates
    `candles_by_symbol.get(symbol, [])` per requested symbol, so this is
    just as dead as the empty case and much easier to misread."""
    assert _feed_can_emit(_simulated({"INFY": [_A_CANDLE]}), ["NIFTY"]) is False


async def test_a_simulated_feed_holding_candles_can_emit():
    """Control. The guard must only refuse a provable dead end -- a
    populated simulated feed is how local development and the replay-style
    dev loop work, and refusing it would break them."""
    assert _feed_can_emit(_simulated({"NIFTY": [_A_CANDLE]}), ["NIFTY"]) is True


async def test_a_non_simulated_feed_is_assumed_live():
    """Control, and the limit of what this guard may claim.

    Only `SimulatedFeed` can be answered statically, because it replays a
    dict it was handed. A real feed's silence is a fact about the market,
    not about the object -- refusing to start one would make this guard the
    cause of the outage it exists to report.
    """

    class _RealFeed:
        async def subscribe(self, symbols):  # pragma: no cover - never called
            yield

        async def unsubscribe(self, symbols):  # pragma: no cover - never called
            pass

    assert _feed_can_emit(_RealFeed(), ["NIFTY"]) is True
    assert _feed_can_emit(_RealFeed(), []) is True, (
        "a live feed with nothing subscribed yet is still a live feed"
    )


async def test_no_symbols_against_a_simulated_feed_cannot_emit():
    """Control. `WORKER_SYMBOLS` empty already logs its own warning; the
    bridge still must not be supervised, because subscribing to nothing
    yields nothing."""
    assert _feed_can_emit(_simulated({"NIFTY": [_A_CANDLE]}), []) is False
