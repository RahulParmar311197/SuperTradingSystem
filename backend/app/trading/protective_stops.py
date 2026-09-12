"""Broker-side stop orders that actually enforce a position's stop
(blueprint §57, §60).

`PositionRecord.stop` is a price. On its own it is inert: the paper engine
(`app/paper/engine.py`) checks it against every candle it is fed, but a
live position has no candle loop and nothing else in the system ever
looked at it, so a stop attached to a real order did nothing whatsoever --
price could run straight through it and the position stayed open.

The fix is the one a real trading desk uses: as soon as an entry fills,
put a protective stop *at the broker*. It rests there independently of
this process, so it still works when the API is restarting, wedged, or
disconnected -- which is exactly when a local watcher loop would fail and
exactly when you most need the stop. `_ensure_protective_stop` is the one
place that keeps that resting order in step with the position it guards.
"""

from __future__ import annotations

import logging
import uuid

from app.brokers.base import Broker, OrderRequest
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.trading.position_manager import PositionRecord

logger = logging.getLogger("trading.protective_stops")


async def cancel_protective_stop(broker: Broker, position: PositionRecord) -> None:
    """Cancel the resting stop guarding `position`, if there is one.

    Tolerant by design: a stop that has already fired, or that the broker
    no longer knows about, is not an error -- the goal state is "no live
    protective order for this position", and both of those already are it.
    """
    order_id = position.protective_order_id
    if order_id is None:
        return
    position.protective_order_id = None
    try:
        await broker.cancel_order(order_id)
    except Exception:  # noqa: BLE001 - a broker that cannot cancel must not break the caller
        logger.warning("Could not cancel protective stop %s for %s", order_id, position.symbol, exc_info=True)


async def ensure_protective_stop(broker: Broker, position: PositionRecord) -> str | None:
    """Make the broker's resting stop match `position`'s current stop and
    size, and return the live protective order id (or `None`).

    Called after every fill on a position, because all three of the things
    this order is derived from can change with one: the quantity (adding
    to a position leaves the old stop covering only part of it), the
    direction (a flip needs the stop on the other side), and whether a
    position exists at all (a close must not leave a stop resting against
    nothing -- at a real broker that becomes a *new naked position* in the
    opposite direction the moment it fires).

    Replace-then-place rather than modify: `Broker.modify_order` is
    optional surface that not every adapter implements meaningfully, while
    cancel and place are the two calls every adapter must support.
    """
    await cancel_protective_stop(broker, position)

    if not position.is_open or position.stop is None:
        return None

    # The stop closes the position, so it faces the other way.
    exit_direction = Direction.SHORT if position.is_long else Direction.LONG
    result = await broker.place_order(
        OrderRequest(
            idempotency_key=f"protective:{position.account_id}:{position.symbol}:{uuid.uuid4()}",
            symbol=position.symbol,
            direction=exit_direction,
            # SL_M, not SL: once a stop triggers, getting out matters more
            # than the price, and a stop-limit can go unfilled in exactly
            # the fast market that triggered it -- leaving the position
            # open with its protection already spent.
            order_type=OrderType.SL_M,
            quantity=abs(position.quantity),
            trigger_price=position.stop,
        )
    )
    if result.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
        logger.error(
            "Broker rejected the protective stop for %s at %s: %s",
            position.symbol,
            position.stop,
            result.rejection_reason,
        )
        return None

    position.protective_order_id = result.broker_order_id
    return result.broker_order_id
