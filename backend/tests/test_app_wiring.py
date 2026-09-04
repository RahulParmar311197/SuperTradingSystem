"""Sanity checks for app-level wiring: request-id propagation, metrics
endpoint, and the unhandled-exception handler."""

from fastapi.testclient import TestClient

from app.main import app


def test_request_id_header_present_on_every_response():
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers.get("x-request-id")


def test_request_id_is_echoed_back_when_supplied():
    with TestClient(app) as client:
        r = client.get("/", headers={"X-Request-ID": "fixed-id-123"})
        assert r.headers["x-request-id"] == "fixed-id-123"


def test_metrics_endpoint_exposes_prometheus_format():
    with TestClient(app) as client:
        client.get("/")  # generate at least one request metric
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "http_requests_total" in r.text


def test_cors_denies_by_default():
    with TestClient(app) as client:
        r = client.get("/", headers={"Origin": "http://evil.example.com"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
