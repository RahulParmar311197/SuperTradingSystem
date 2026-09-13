"""Fitting externally-supplied text into fixed-width columns."""

from __future__ import annotations

_ELLIPSIS = "…"


def clip(value: str | None, limit: int) -> str | None:
    """`value` shortened to at most `limit` characters, marked where cut.

    Postgres `VARCHAR(n)` does not truncate — it raises
    `StringDataRightTruncation` and takes the whole transaction with it.
    That is the right default for data the system owns, and the wrong one
    for diagnostic text a *broker* wrote, which has no length the broker
    ever promised. Measured on the live order path: a 789-character
    rejection reason (an HTML error page, which is what
    `_extract_error_message` returns for a non-JSON body — a proxy 502 in
    front of the broker produces exactly that) made `POST /orders` raise
    out of `persist_order`, leaving **no order row at all** — no status,
    no reason, and no ORDER_REJECTED notification. A 996-character stop
    rejection was worse: the entry had already filled and was journaled
    MONITORING, the broker had refused the protective stop, and the
    notification telling the account holder their position has no
    stop-loss was the write that failed.

    Losing the tail of a diagnostic message is not comparable to losing
    the message, the order row, and the alert. So clip, and mark the cut
    so nobody reads a truncated reason as the whole story.

    Only ever for prose. Never clip an identifier: a shortened order id is
    not a shorter name for the same order, it is a wrong one that silently
    fails to match at the broker. Where an id does not fit, the column is
    too narrow and belongs in a migration (see
    `positions.protective_order_id`).
    """
    if value is None or len(value) <= limit:
        return value
    if limit <= 0:
        return ""
    return value[: limit - 1] + _ELLIPSIS
