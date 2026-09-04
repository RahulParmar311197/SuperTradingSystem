"""Tests for the trading-halt and price-age helpers in app.core.redis."""

import uuid

from app.core.redis import (
    account_halt_reason,
    get_price_age_seconds,
    halt_account,
    heartbeat,
    list_halted_accounts,
    resume_account,
    set_latest_price,
    worker_is_alive,
)


async def test_price_age_is_none_when_never_set(require_infra):
    symbol = f"NOPRICE{uuid.uuid4().hex[:8]}"
    assert await get_price_age_seconds(symbol) is None


async def test_price_age_is_near_zero_right_after_a_set(require_infra):
    symbol = f"FRESH{uuid.uuid4().hex[:8]}"
    await set_latest_price(symbol, 100.0)
    age = await get_price_age_seconds(symbol)
    assert age is not None
    assert 0 <= age < 2


async def test_halt_and_resume_account(require_infra):
    account_id = f"halttest-{uuid.uuid4().hex[:8]}"
    assert await account_halt_reason(account_id) is None

    await halt_account(account_id, "test halt")
    assert await account_halt_reason(account_id) == "test halt"

    await resume_account(account_id)
    assert await account_halt_reason(account_id) is None


async def test_worker_is_alive_only_after_a_heartbeat(require_infra):
    name = f"testworker-{uuid.uuid4().hex[:8]}"
    assert await worker_is_alive(name) is False

    await heartbeat(name)
    assert await worker_is_alive(name) is True


async def test_list_halted_accounts_includes_only_currently_halted_ones(require_infra):
    account_a = f"halted-{uuid.uuid4().hex[:8]}"
    account_b = f"halted-{uuid.uuid4().hex[:8]}"
    assert account_a not in await list_halted_accounts()

    try:
        await halt_account(account_a, "reason A")
        await halt_account(account_b, "reason B")
        halted = await list_halted_accounts()
        assert halted[account_a] == "reason A"
        assert halted[account_b] == "reason B"

        await resume_account(account_a)
        halted = await list_halted_accounts()
        assert account_a not in halted
        assert halted[account_b] == "reason B"
    finally:
        await resume_account(account_a)
        await resume_account(account_b)
