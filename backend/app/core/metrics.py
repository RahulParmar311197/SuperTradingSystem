"""Prometheus metrics (blueprint §72). Exposed at GET /metrics."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "path"]
)
ORDER_COUNT = Counter("orders_total", "Total orders submitted", ["status"])
RISK_REJECTION_COUNT = Counter("risk_rejections_total", "Total orders rejected by the risk engine")
# A derived-timeframe candle whose bucket held fewer base candles than the
# period has minutes. The bar IS written -- see
# `CandleWorker._derive_timeframe` for why the aggregate of the minutes
# that traded is the correct bar -- so this counts incompleteness, not a
# skip, and it is not an error: the worker is tick-driven, so a minute in
# which nothing traded leaves no base candle, which is routine on an
# illiquid instrument.
#
# Renamed from `derived_candles_skipped_total`, deliberately and with the
# exported name changed too. It used to count bars the worker refused to
# write; keeping a metric called "skipped" for something no longer skipped
# would have been a label that lies about what it measures. Nothing is
# scraping this yet, so there are no dashboards to migrate.
#
# Non-zero and climbing on an instrument means its higher timeframes are
# built from partial data: either that instrument barely trades, or ticks
# are being lost. The two are indistinguishable from here.
# A closed base candle thrown away because its symbol has no configured
# `Instrument` row. Labelled by symbol because that is the thing an
# operator has to add to `WORKER_INSTRUMENT_IDS` -- an unlabelled total
# would say "some symbol is misconfigured" and leave them grepping.
#
# Non-zero at all means a misconfiguration, not a degraded condition:
# every downstream reader (ScannerWorker, AutoTradeSupervisor, backtests)
# works from the `candles` table, so a symbol counting here is invisible
# to all of them no matter how long the worker runs.
CANDLE_DROPPED_UNKNOWN_INSTRUMENT = Counter(
    "candles_dropped_unknown_instrument_total",
    "Closed candles discarded because the symbol has no configured instrument id",
    ["symbol"],
)


DERIVED_CANDLE_INCOMPLETE = Counter(
    "derived_candles_incomplete_total",
    "Derived-timeframe candles built from a bucket missing at least one base candle",
    ["timeframe"],
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
