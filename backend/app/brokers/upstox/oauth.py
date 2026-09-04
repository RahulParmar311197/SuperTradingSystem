"""Upstox OAuth2 (authorization-code, no PKCE) — blueprint §52, §69.

Confirmed against Upstox's public developer docs and community posts as of
2026-09 (search snippets only — this sandbox's network egress to
upstox.com is blocked, so these could not be fetched and cross-checked
directly). **Verify against the live docs / Postman collection before
connecting a real account**, per the blueprint's standing instruction not
to trust hardcoded broker specifics.

Flow:
  1. Send the user to `build_authorization_url(...)`.
  2. Upstox redirects back to your `redirect_uri` with `?code=...`.
  3. Exchange that code with `exchange_code_for_token(...)` for an
     access_token, which is what `UpstoxBroker` needs.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

UPSTOX_AUTH_BASE_URL = "https://api.upstox.com/v2"


def build_authorization_url(client_id: str, redirect_uri: str, state: str | None = None) -> str:
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri}
    if state:
        params["state"] = state
    return f"{UPSTOX_AUTH_BASE_URL}/login/authorization/dialog?{urlencode(params)}"


async def exchange_code_for_token(
    client_id: str, client_secret: str, redirect_uri: str, code: str, http_client: httpx.AsyncClient | None = None
) -> dict:
    """Returns Upstox's raw token response (at minimum: access_token)."""
    client = http_client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.post(
            f"{UPSTOX_AUTH_BASE_URL}/login/authorization/token",
            headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        return response.json()
    finally:
        if http_client is None:
            await client.aclose()
