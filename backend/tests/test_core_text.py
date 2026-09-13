"""`clip` — fitting text a broker wrote into a column we declared."""

import pytest

from app.core.text import clip


def test_a_short_string_is_returned_unchanged():
    # Control: this must be identity for everything that already fits, or
    # every message in the system would grow an ellipsis.
    assert clip("short", 100) == "short"


def test_a_string_exactly_at_the_limit_is_untouched():
    # The off-by-one that would quietly mangle the most common case.
    assert clip("x" * 500, 500) == "x" * 500


def test_none_stays_none():
    # `rejection_reason` is nullable and usually null; clipping must not
    # turn "no reason" into an empty string.
    assert clip(None, 500) is None


def test_an_over_long_string_is_cut_to_the_limit():
    result = clip("x" * 900, 500)
    assert result is not None
    assert len(result) == 500


def test_the_cut_is_marked():
    # A truncated broker error read as the whole error is its own bug.
    result = clip("x" * 900, 500)
    assert result is not None and result.endswith("…")
    assert result[:499] == "x" * 499


@pytest.mark.parametrize("limit", [0, -1])
def test_a_zero_or_negative_limit_yields_the_empty_string(limit):
    # No column is declared this way; the branch exists so the helper can
    # never itself raise on the path whose whole point is not raising.
    assert clip("anything", limit) == ""


def test_the_limits_are_read_off_the_columns_they_protect():
    # The bug was two places disagreeing about one width. If these are
    # ever restated as literals and a column is widened, this fails.
    from app.database.models.notifications import Notification
    from app.database.models.trading import Order
    from app.notifications.service import _BODY_LIMIT, _TITLE_LIMIT
    from app.trading.persistence import _REJECTION_REASON_LIMIT

    assert _REJECTION_REASON_LIMIT == Order.__table__.c.rejection_reason.type.length
    assert _TITLE_LIMIT == Notification.__table__.c.title.type.length
    assert _BODY_LIMIT == Notification.__table__.c.body.type.length


def test_a_broker_order_id_column_is_wide_enough_for_a_broker_order_id():
    # Not a `clip` case at all, and that is the point: an id is the one
    # kind of value truncation must never touch, because a shortened order
    # id silently matches nothing at the broker. `positions.
    # protective_order_id` and `orders.broker_order_id` hold the same
    # value; before the fix they were declared 64 and 128, so an id in
    # between was accepted by the order journal and rejected by the
    # position journal -- after the stop was already resting at the venue.
    from app.database.models.trading import Order, Position

    assert (
        Position.__table__.c.protective_order_id.type.length
        >= Order.__table__.c.broker_order_id.type.length
    )
