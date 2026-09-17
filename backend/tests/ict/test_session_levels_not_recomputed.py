"""The ICT engine used to recompute session levels and throw them away.

`ICTEngine.analyze` filled `ICTContext.session_levels` with
`detect_session_levels(candles, "day") + detect_session_levels(candles, "week")`
on every call, and nothing read it — not `app/strategy/evaluator.py` (which
reads `current_kill_zones`), not `GET /charts/{id}/smc` (which exposes the
kill zones and the opening range), not `app/ai/context_builder.py`.

`SMCEngine.analyze` computes the identical pools into
`SMCContext.liquidity_pools`, which *is* what the evaluator reads. Measured
byte-identical: 46 pools each, same set.

Measured cost of the duplicate, by toggling it off:

    500 candles  0.70ms -> 0.38ms   (45.6% of the call)
    2000 candles 3.02ms -> 1.61ms   (46.8%)
    6000 candles 8.70ms -> 4.35ms   (50.0%)

paid once per (user, strategy, instrument) engine on every pass of the 60s
autonomous loop. This is a performance fix, not a correctness one: no
output changes, which is what the second test here pins.
"""

from datetime import datetime, timedelta, timezone

import app.smc.engine as smc_engine_module
from app.ict.engine import ICTEngine
from app.smc.engine import SMCEngine
from app.smc.types import Candle, LiquiditySourceType

BASE = datetime(2026, 9, 1, 3, 45, tzinfo=timezone.utc)

_SESSION_KINDS = {
    LiquiditySourceType.PREVIOUS_DAY_HIGH,
    LiquiditySourceType.PREVIOUS_DAY_LOW,
    LiquiditySourceType.PREVIOUS_WEEK_HIGH,
    LiquiditySourceType.PREVIOUS_WEEK_LOW,
}


def _candles(n: int = 2000) -> list[Candle]:
    out = []
    for i in range(n):
        price = 100.0 + (i % 37) * 0.5
        out.append(Candle(BASE + timedelta(minutes=15 * i), price, price + 1, price - 1, price + 0.25, 1000))
    return out


def test_one_analyze_pass_detects_session_levels_once_not_twice(monkeypatch):
    """The duplication, pinned by call count rather than by a timing
    assertion, which would be flaky in CI.

    Patched in whichever engine modules import the name, so this measures
    the real thing before and after: four calls before (ICT's day + week,
    SMC's day + week), two after.
    """
    import app.ict.engine as ict_engine_module

    calls: list[str] = []
    real = smc_engine_module.detect_session_levels

    def counting(candles, period="day", **kwargs):
        calls.append(period)
        return real(candles, period, **kwargs)

    for module in (smc_engine_module, ict_engine_module):
        if hasattr(module, "detect_session_levels"):
            monkeypatch.setattr(module, "detect_session_levels", counting)

    candles = _candles()
    ICTEngine().analyze(candles)
    SMCEngine().analyze(candles)

    assert len(calls) == 2, f"session levels detected {len(calls)} times for one pass: {calls}"
    assert sorted(calls) == ["day", "week"]


def test_the_same_session_pools_are_still_reachable_from_the_smc_context():
    # The property that makes the removal safe: everything the ICT engine
    # was computing is still produced, by the engine whose output consumers
    # actually read.
    candles = _candles()
    pools = [p for p in SMCEngine().analyze(candles).liquidity_pools if p.source_type in _SESSION_KINDS]

    assert pools, "session pools must still be produced somewhere"
    assert {p.source_type for p in pools} == _SESSION_KINDS


def test_the_ict_context_still_carries_what_consumers_read():
    # Control: removing the dead field must not disturb the two that are
    # read — `current_kill_zones` by the evaluator, the opening range by
    # GET /charts/{id}/smc.
    context = ICTEngine().analyze(_candles())

    assert isinstance(context.current_kill_zones, list)
    assert context.opening_ranges, "the opening range is read by the charts endpoint"
    assert context.current_opening_range is not None
    assert not hasattr(context, "session_levels"), "the dead field should be gone, not merely unused"
