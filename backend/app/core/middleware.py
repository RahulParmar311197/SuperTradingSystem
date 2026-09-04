"""Request-id + metrics middleware (blueprint §72)."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import request_id_var
from app.core.metrics import REQUEST_COUNT, REQUEST_LATENCY


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id (reusing an inbound X-Request-ID if present),
    makes it available to logging via a contextvar, echoes it back on the
    response, and records request count/latency metrics."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        path = request.url.path

        try:
            response = await call_next(request)
        except Exception:
            REQUEST_COUNT.labels(request.method, path, "500").inc()
            raise
        finally:
            request_id_var.reset(token)

        duration = time.perf_counter() - start
        REQUEST_LATENCY.labels(request.method, path).observe(duration)
        REQUEST_COUNT.labels(request.method, path, str(response.status_code)).inc()
        response.headers["X-Request-ID"] = request_id
        return response
