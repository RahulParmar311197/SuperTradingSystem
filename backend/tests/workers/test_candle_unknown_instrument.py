"""A symbol with no configured instrument id had its candles silently dropped.

`CandleWorker._on_candle_closed` skips `upsert_candles` when
`instrument_ids` has no entry for the symbol — correctly, there is no row
to hang the candle on. What it did not do was say so. Measured before this
change, feeding one closed candle for an unmapped symbol:

    stored:       []
    published to: channel:chart:INFY:1m
    log records:  []

So a deployment that sets `WORKER_SYMBOLS` but omits a symbol from
`WORKER_INSTRUMENT_IDS` builds a candle a minute, streams every one of
them, stores none, and reports nothing. The streaming is what makes it
hard to see: `/ws/chart` takes `instrument_id` as a `str`, so a client
subscribing by symbol really does receive these, and every visible sign
says the pipeline works.

It does not. `ScannerWorker`, `AutoTradeSupervisor`, the backtest engine
and the replay engine all read the `candles` table. A symbol that never
reaches it is invisible to all of them for as long as the deployment runs.

Same family as the round that found the market-data bridge restarting
forever without ever yielding a tick: a worker that looks busy and
achieves nothing.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from app.smc.types import Candle
from app.workers.candle_worker import CandleWorker

CANDLE = Candle(datetime(2026, 9, 18, 6, 0, tzinfo=timezone.utc), 100.0, 101.0, 99.0, 100.5, 1000.0)


class _Recorder:
    """Captures what the worker stored and published."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.stored: list[uuid.UUID] = []

    async def publish(self, channel: str, payload: dict) -> None:
        self.published.append(channel)

    async def upsert(self, db, instrument_id, timeframe, candles) -> None:
        self.stored.append(instrument_id)


async def _feed(worker: CandleWorker, symbol: str, times: int = 1) -> _Recorder:
    recorder = _Recorder()
    with patch("app.workers.candle_worker.publish", recorder.publish), patch(
        "app.workers.candle_worker.upsert_candles", recorder.upsert
    ):
        for _ in range(times):
            await worker._on_candle_closed(symbol, CANDLE)
    return recorder


# --- the drop must be visible --------------------------------------------


async def test_an_unmapped_symbol_is_reported_not_silently_dropped(caplog):
    """Behavioural proof. The whole finding: nothing was logged at all."""
    worker = CandleWorker({}, base_timeframe="1m")
    with caplog.at_level(logging.ERROR, logger="workers.candle"):
        recorder = await _feed(worker, "INFY")

    assert recorder.stored == [], "an unmapped symbol genuinely cannot be stored"
    assert caplog.records, "the drop must not be silent"
    message = caplog.records[0].getMessage()
    assert "INFY" in message
    assert "WORKER_INSTRUMENT_IDS" in message, f"the remedy must be named: {message}"


async def test_the_warning_names_what_stops_working(caplog):
    """Behavioural proof. "Could not store a candle" understates it — the
    symbol becomes invisible to every consumer that reads the table, which
    is what an operator needs to weigh."""
    worker = CandleWorker({}, base_timeframe="1m")
    with caplog.at_level(logging.ERROR, logger="workers.candle"):
        await _feed(worker, "INFY")

    message = caplog.records[0].getMessage().lower()
    assert "scanner" in message and "backtest" in message, message


async def test_it_warns_once_per_symbol_not_once_per_candle(caplog):
    """Behavioural proof. At one base candle a minute this would be 1,440
    identical lines a day per misconfigured symbol — which is how a real
    warning gets filtered out of a log."""
    worker = CandleWorker({}, base_timeframe="1m")
    with caplog.at_level(logging.ERROR, logger="workers.candle"):
        await _feed(worker, "INFY", times=5)

    assert len(caplog.records) == 1, [r.getMessage() for r in caplog.records]


async def test_each_symbol_gets_its_own_warning(caplog):
    """Control for the deduplication above. Suppressing repeats must not
    suppress a *different* symbol's first report — that would trade one
    silence for another."""
    worker = CandleWorker({}, base_timeframe="1m")
    with caplog.at_level(logging.ERROR, logger="workers.candle"):
        await _feed(worker, "INFY", times=3)
        await _feed(worker, "TCS", times=3)

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2, messages
    assert sorted(m.split(" for ")[1].split(":")[0] for m in messages) == ["INFY", "TCS"], messages


async def test_every_dropped_candle_is_counted_even_though_only_one_is_logged():
    """Behavioural proof. The log line is deduplicated; the metric must not
    be, or there is no way to see how much data was lost."""
    from app.core.metrics import CANDLE_DROPPED_UNKNOWN_INSTRUMENT

    symbol = f"METRIC{uuid.uuid4().hex[:6].upper()}"
    before = CANDLE_DROPPED_UNKNOWN_INSTRUMENT.labels(symbol)._value.get()

    worker = CandleWorker({}, base_timeframe="1m")
    await _feed(worker, symbol, times=4)

    after = CANDLE_DROPPED_UNKNOWN_INSTRUMENT.labels(symbol)._value.get()
    assert after - before == 4


# --- and what must keep working ------------------------------------------


async def test_the_candle_is_still_streamed_for_an_unmapped_symbol(caplog):
    """Control, and a guard against over-fixing. Dropping the publish would
    have been an easy "tidy-up" here — but `/ws/chart` takes its
    `instrument_id` as a `str`, so a client subscribed by symbol really is
    receiving these. Storage is what silently fails; streaming is not."""
    worker = CandleWorker({}, base_timeframe="1m")
    with caplog.at_level(logging.ERROR, logger="workers.candle"):
        recorder = await _feed(worker, "INFY", times=3)

    assert recorder.published == ["channel:chart:INFY:1m"] * 3


async def test_a_mapped_symbol_stores_and_says_nothing(caplog):
    """Control, and the one that stops this becoming "warn about
    everything". The ordinary configured case must be silent."""
    instrument_id = uuid.uuid4()
    worker = CandleWorker({"INFY": instrument_id}, base_timeframe="1m")
    with caplog.at_level(logging.ERROR, logger="workers.candle"):
        recorder = await _feed(worker, "INFY", times=3)

    assert recorder.stored == [instrument_id] * 3
    assert caplog.records == []


# --- the startup check, where an operator can still act -------------------


async def test_the_worker_entrypoint_reports_unmapped_symbols_at_startup(caplog, monkeypatch):
    """Behavioural proof at the call site.

    The runtime warning above only fires once a candle actually closes,
    which is minutes later and buried in a running log. This is the first
    screen of output, when someone is still watching.

    Driven through the real `main()` rather than a helper: the startup
    logging happens before it settles into `stop_event.wait()`, so a
    bounded run reaches it. Bounded on purpose — `main()` never returns on
    its own, and a test that hangs is a worse signal than one that fails.
    """
    import app.workers.main as worker_main

    monkeypatch.setenv("WORKER_SYMBOLS", "INFY,TCS")
    monkeypatch.setenv("WORKER_INSTRUMENT_IDS", f"INFY={uuid.uuid4()}")

    with caplog.at_level(logging.ERROR, logger="workers.main"):
        task = asyncio.create_task(worker_main.main())
        await asyncio.sleep(0.2)
        task.cancel()
        _done, pending = await asyncio.wait([task], timeout=2.0)
        assert not pending, "main() did not stop when cancelled"

    reports = [r.getMessage() for r in caplog.records if "No instrument id configured" in r.getMessage()]
    assert len(reports) == 1, [r.getMessage() for r in caplog.records]
    assert "TCS" in reports[0] and "WORKER_INSTRUMENT_IDS" in reports[0], reports[0]
    # Not "INFY=" -- that is the env-var spelling and appears in no log
    # line, so asserting its absence would have proved nothing. The symbol
    # itself is what an over-broad check would wrongly name here.
    assert "INFY" not in reports[0], f"the configured symbol must not be reported: {reports[0]}"


async def test_a_fully_mapped_entrypoint_reports_nothing(caplog, monkeypatch):
    """Control for the startup check. With every symbol mapped there must
    be no unmapped-symbol error, or the check is noise."""
    import app.workers.main as worker_main

    monkeypatch.setenv("WORKER_SYMBOLS", "INFY")
    monkeypatch.setenv("WORKER_INSTRUMENT_IDS", f"INFY={uuid.uuid4()}")

    with caplog.at_level(logging.ERROR, logger="workers.main"):
        task = asyncio.create_task(worker_main.main())
        await asyncio.sleep(0.2)
        task.cancel()
        _done, pending = await asyncio.wait([task], timeout=2.0)
        assert not pending

    assert not any("WORKER_INSTRUMENT_IDS" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
