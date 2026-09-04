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


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
