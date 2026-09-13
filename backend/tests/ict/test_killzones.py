"""A kill zone is a UTC window, so it must be read against a UTC hour.

`KillZone` declares `start_hour_utc`/`end_hour_utc` and this module's
docstring calls them UTC windows -- but `active_kill_zones` read
`candle.timestamp.hour`, which is the hour in whatever zone the timestamp
carries. The same instant then landed in different zones depending only on
how the caller spelled it.

Measured before the fix, on one instant Python itself reports as equal:

    2026-01-05T03:45:00+00:00  ->  ['ASIAN']
    2026-01-05T09:15:00+05:30  ->  ['LONDON']

IST is the natural spelling for an NSE client, and
`POST /paper/{id}/candle` takes the timestamp straight from the request
body, so this is reachable without anything unusual. Across the NSE
session three of four sampled times came out wrong: 09:15 reported LONDON
when it is ASIAN, 13:00 reported NEW_YORK when it is LONDON, 15:15
reported LONDON_CLOSE when it is LONDON.

It is not a display detail. `ConditionType.SESSION`
(`app/strategy/evaluator.py`) matches a strategy's session condition
against `ict.current_kill_zones`, so a strategy restricted to the London
kill zone was firing during the Asian session -- silently, and only for
clients who send their own timezone.
"""

from __future__ import annotations

import os
import time as time_module
from datetime import datetime, timedelta, timezone

import pytest

from app.ict.engine import ICTConfig, ICTEngine
from app.ict.killzones import DEFAULT_KILL_ZONES, KillZone, active_kill_zones
from app.smc.types import Candle

_IST = timezone(timedelta(hours=5, minutes=30))


def _candle(ts: datetime) -> Candle:
    return Candle(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=10.0)


# (hour, minute) in IST across an NSE session, and the zone that instant
# genuinely falls in once converted to UTC.
_NSE_SESSION = [
    ((9, 15), ["ASIAN"]),
    ((11, 0), []),
    ((13, 0), ["LONDON"]),
    ((15, 15), ["LONDON"]),
]


@pytest.mark.parametrize("clock,expected", _NSE_SESSION)
def test_an_ist_timestamp_lands_in_the_zone_its_instant_belongs_to(clock, expected):
    """Behavioural proof, stated as the instant's real zone rather than as
    'the same as UTC', so it fails if both spellings agree on a wrong
    answer."""
    hour, minute = clock
    ts = datetime(2026, 1, 5, hour, minute, tzinfo=_IST)
    assert active_kill_zones(_candle(ts)) == expected


@pytest.mark.parametrize("clock,_expected", _NSE_SESSION)
def test_the_same_instant_gives_the_same_zones_however_it_is_spelled(clock, _expected):
    """Behavioural proof of the invariant behind it: the zone is a property
    of the instant, not of the caller's timezone."""
    hour, minute = clock
    ist = datetime(2026, 1, 5, hour, minute, tzinfo=_IST)
    utc = ist.astimezone(timezone.utc)
    assert ist == utc, "fixture: these must be the same instant"
    assert active_kill_zones(_candle(ist)) == active_kill_zones(_candle(utc))


def test_a_naive_timestamp_is_read_as_utc_not_as_machine_local_time():
    """Control on the fix's own trap.

    `astimezone()` on a naive datetime assumes the *machine's* zone, which
    would be the same bug one layer down -- and invisible on a
    UTC-configured CI box. The process TZ is moved to Asia/Kolkata for this
    test, so a fix that reached for `astimezone()` alone fails here.
    """
    naive = datetime(2026, 1, 5, 3, 45)
    before = active_kill_zones(_candle(naive))
    assert before == ["ASIAN"]

    original = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time_module.tzset()
    try:
        assert active_kill_zones(_candle(naive)) == ["ASIAN"], (
            "a naive timestamp changed meaning when the machine's timezone did"
        )
    finally:
        if original is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = original
        time_module.tzset()


def test_utc_timestamps_are_unchanged():
    """Control: the existing, documented behaviour for UTC input must be
    exactly as before -- this fix is a normalisation, not a re-timing of
    the windows themselves."""
    for hour, expected in ((0, ["ASIAN"]), (3, ["ASIAN"]), (8, ["LONDON"]), (13, ["NEW_YORK"]), (15, ["LONDON_CLOSE"]), (20, [])):
        ts = datetime(2026, 1, 5, hour, 0, tzinfo=timezone.utc)
        assert active_kill_zones(_candle(ts)) == expected, f"{hour:02d}:00 UTC"


def test_a_zone_that_wraps_past_midnight_still_wraps():
    """Control on the other branch of `KillZone.contains`, which the
    normalisation runs through just the same."""
    overnight = [KillZone("OVERNIGHT", 22.0, 2.0)]
    for hour, inside in ((22, True), (23, True), (1, True), (2, False), (12, False)):
        ts = datetime(2026, 1, 5, hour, 0, tzinfo=timezone.utc)
        assert (active_kill_zones(_candle(ts), overnight) == ["OVERNIGHT"]) is inside, f"{hour:02d}:00"


def test_the_engine_reports_the_zone_a_strategy_will_be_gated_on():
    """Behavioural proof one layer up, where it actually matters.

    `ICTEngine.analyze` fills `current_kill_zones`, and
    `ConditionType.SESSION` matches a strategy's session name against that
    list. This is the value a London-only strategy was matching against
    while the market was in the Asian session.
    """
    # `analyze` reads `candles[-1]`, so the series is built *backwards*
    # from the instant under test: 09:15 IST is 03:45 UTC, inside ASIAN
    # (00:00-04:00 UTC). Building forwards from 09:15 instead put the last
    # candle at 09:45 IST = 04:15 UTC, past the window -- which is the
    # correct answer for that candle, and not the one this test is about.
    last = datetime(2026, 1, 5, 9, 15, tzinfo=_IST)
    candles = [_candle(last - timedelta(minutes=15 * i)) for i in range(2, -1, -1)]
    assert candles[-1].timestamp == last
    context = ICTEngine(ICTConfig(kill_zones=list(DEFAULT_KILL_ZONES))).analyze(candles)

    assert context.current_kill_zones == ["ASIAN"]
    assert context.in_kill_zone("ASIAN") is True
    assert context.in_kill_zone("LONDON") is False
