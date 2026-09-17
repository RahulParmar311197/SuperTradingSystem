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
# A derived-timeframe candle that was not written because its bucket was
# missing at least one base candle. This is not an error -- the worker is
# tick-driven, so a minute in which nothing traded leaves no base candle,
# and skipping is the deliberate conservative choice (see
# `CandleWorker._derive_timeframe`). It is, however, a hole in the series
# the strategies read, and it used to be a bare `return` that no operator
# could see. Non-zero and climbing on an instrument means its higher
# timeframes are incomplete.
DERIVED_CANDLE_SKIPPED = Counter(
    "derived_candles_skipped_total",
    "Derived-timeframe candles not written because their bucket had missing base candles",
    ["timeframe"],
)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
