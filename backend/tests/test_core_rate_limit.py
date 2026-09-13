"""Which address the per-IP rate limiter actually keys on.

`request.client.host` is the socket peer. Deployed exactly as this repo
documents -- `infrastructure/nginx/nginx.conf.example`, blueprint §105 --
that peer is nginx, identically for every user, so the per-IP limiter on
login/register collapses into one global bucket. Measured before the fix:
twelve requests from twelve distinct client addresses through one proxy,
limit 10/minute, and the eleventh and twelfth came back 429.

The security half matters as much as the fix: consulting
`X-Forwarded-For` is opt-in, and reads back from the *right*. nginx
appends the peer it saw, so anything a client forges sits to the left of
it. A limiter that read the leftmost entry would be no limiter at all --
and worse than none, since an attacker could claim a victim's address and
lock them out of login.
"""

import pytest
from starlette.requests import Request

from app.core.rate_limit import client_ip


def _request(peer: str | None, forwarded: str | None = None) -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request(
        {
            "type": "http",
            "headers": headers,
            "client": (peer, 51234) if peer is not None else None,
        }
    )


# --- no proxy declared: the header is not evidence of anything ------------


def test_the_peer_is_used_when_no_proxy_is_declared():
    # Control: the default (0 hops) must behave exactly as the code did
    # before this helper existed.
    assert client_ip(_request("198.51.100.7"), 0) == "198.51.100.7"


def test_a_forged_header_is_ignored_when_no_proxy_is_declared():
    # The whole reason the setting defaults to 0. With nothing in front of
    # the process, honouring this header would let every client pick its
    # own bucket -- strictly worse than the bug being fixed.
    assert client_ip(_request("198.51.100.7", "203.0.113.1"), 0) == "198.51.100.7"


def test_a_missing_peer_is_named_rather_than_crashing():
    assert client_ip(_request(None), 0) == "unknown"


# --- one declared proxy: the bug this fixes -------------------------------


def test_one_declared_proxy_reads_the_address_it_observed():
    # nginx's $proxy_add_x_forwarded_for on a direct client leaves exactly
    # one entry: the address nginx itself saw.
    assert client_ip(_request("10.0.0.2", "203.0.113.9"), 1) == "203.0.113.9"


def test_distinct_clients_behind_one_proxy_get_distinct_keys():
    # The regression proper. Same peer (the proxy) for both; before the
    # fix both mapped to "10.0.0.2" and shared a bucket.
    first = client_ip(_request("10.0.0.2", "203.0.113.1"), 1)
    second = client_ip(_request("10.0.0.2", "203.0.113.2"), 1)
    assert first != second


def test_a_client_cannot_forge_its_address_past_a_declared_proxy():
    # The client sent "X-Forwarded-For: 203.0.113.99" hoping to be limited
    # as (or to exhaust the budget of) someone else; nginx appended the
    # address it actually saw. Counting from the right ignores the forgery.
    forged = _request("10.0.0.2", "203.0.113.99, 198.51.100.4")
    assert client_ip(forged, 1) == "198.51.100.4"


def test_a_long_forged_chain_still_resolves_to_the_observed_address():
    chain = ", ".join(f"203.0.113.{i}" for i in range(20)) + ", 198.51.100.4"
    assert client_ip(_request("10.0.0.2", chain), 1) == "198.51.100.4"


# --- more than one proxy, and misconfiguration ----------------------------


def test_two_declared_proxies_count_back_two():
    # CDN in front of nginx: each appends what it saw, so the client's real
    # address is the second entry from the right.
    header = "203.0.113.99, 198.51.100.4, 10.0.0.9"
    assert client_ip(_request("10.0.0.2", header), 2) == "198.51.100.4"


@pytest.mark.parametrize("header", [None, "", "   ", "198.51.100.4"])
def test_a_chain_shorter_than_the_declared_hops_falls_back_to_the_peer(header):
    # The operator said two proxies; this request did not come through
    # them. Nothing in the header is attributable, so use the peer, which
    # is real whatever happened upstream.
    assert client_ip(_request("10.0.0.2", header), 2) == "10.0.0.2"


def test_padding_and_empty_entries_do_not_shift_the_count():
    # Control: real proxies emit ", "-joined values, and some append a
    # trailing separator. Neither may move which entry is read.
    assert client_ip(_request("10.0.0.2", " 203.0.113.99 ,  198.51.100.4 ,"), 1) == "198.51.100.4"


# --- end to end through the dependency ------------------------------------


def _app_with_limit(monkeypatch, hops: int, prefix: str):
    from fastapi import Depends, FastAPI

    from app.core import rate_limit as rate_limit_module

    class _Settings:
        rate_limit_enabled = True
        trusted_proxy_hops = hops

    # The suite disables rate limiting globally (see tests/conftest.py),
    # so turn it back on for this one app.
    monkeypatch.setattr(rate_limit_module, "get_settings", lambda: _Settings())

    app = FastAPI()
    guard = rate_limit_module.rate_limit(limit=10, window_seconds=60, key_prefix=prefix)

    @app.post("/login", dependencies=[Depends(guard)])
    async def _login():
        return {"ok": True}

    return app


async def test_distinct_clients_behind_a_proxy_do_not_share_a_bucket(require_infra, monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    app = _app_with_limit(monkeypatch, hops=1, prefix=f"test:{uuid.uuid4().hex[:8]}")
    with TestClient(app) as client:
        codes = [
            client.post("/login", headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
            for i in range(12)
        ]
    # Twelve people signing in within a minute, under a limit of 10 *each*.
    # Before the fix this was [200 x 10, 429, 429]: the platform's whole
    # login endpoint, not one abuser's.
    assert codes == [200] * 12


async def test_one_client_behind_a_proxy_is_still_limited(require_infra, monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    app = _app_with_limit(monkeypatch, hops=1, prefix=f"test:{uuid.uuid4().hex[:8]}")
    with TestClient(app) as client:
        codes = [
            client.post("/login", headers={"X-Forwarded-For": "198.51.100.9"}).status_code
            for i in range(12)
        ]
    # Control: the limiter must still bite, or the test above would pass
    # for the wrong reason -- a limiter that never rejects anyone.
    assert codes == [200] * 10 + [429, 429]


async def test_forging_a_fresh_address_each_try_does_not_evade_the_limit(require_infra, monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    app = _app_with_limit(monkeypatch, hops=1, prefix=f"test:{uuid.uuid4().hex[:8]}")
    with TestClient(app) as client:
        codes = [
            client.post(
                "/login", headers={"X-Forwarded-For": f"10.0.0.{i}, 198.51.100.9"}
            ).status_code
            for i in range(12)
        ]
    # One abuser prepending a new address per attempt, nginx appending the
    # one it saw. If the limiter read the left of the chain this would be
    # twelve 200s and the guard would be decorative.
    assert codes == [200] * 10 + [429, 429]
