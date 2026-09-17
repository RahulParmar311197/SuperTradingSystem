"""The P&L identity, guarded against a future regression.

**No bug was found here.** This file exists because the money math under
every risk gate and every journal number had only two example assertions
behind it — `average_price == 25000.0` and `realized_pnl == 100.0`, both
for a single long round trip in `tests/trading/test_execution.py` — and
nothing at all covered the short side.

Probed before writing it: 20,000 random long/short fill sequences against
a cash-flow ledger, 0 mismatches; and 20,000 random `CostModel`
configurations, 0 cases where friction improved P&L. The identity below is
that probe, seeded and reduced to a size that runs in CI.

Recorded so nobody assumes otherwise: `apply_fill`'s direction-flip branch
is **not reachable in production today**. `POST /orders` computes
`is_reducing` itself from the open position and clamps an opposing order
to `min(quantity, abs(existing.quantity))`, so it can only reduce or
flatten; the paper engine closes with the exact open quantity. The branch
is defensive, and the flip case here exercises it directly rather than
through a caller that cannot produce it.
"""

import random

import pytest

from app.database.models.strategy import Direction
from app.trading.position_manager import PositionManager


def _truth_and_report(fills: list[tuple[Direction, float, float]], mark: float):
    """Run `fills` through the manager and through a ledger that cannot be
    wrong: cash paid/received, plus the mark value of whatever is left."""
    manager = PositionManager()
    cash = 0.0
    net_quantity = 0.0

    for direction, quantity, price in fills:
        manager.apply_fill("acct", "SYM", direction, quantity, price)
        signed = quantity if direction is Direction.LONG else -quantity
        cash -= signed * price
        net_quantity += signed

    position = manager.get("acct", "SYM")
    manager.mark_to_market("acct", "SYM", mark)
    reported = position.realized_pnl + (position.unrealized_pnl if position.is_open else 0.0)
    return cash + net_quantity * mark, reported, position


# --- the identity --------------------------------------------------------


def test_realized_plus_unrealized_equals_the_cash_flow_truth():
    """Seeded, so a failure is reproducible rather than a once-in-a-run flake."""
    rng = random.Random(20260917)

    for _ in range(400):
        fills = [
            (
                rng.choice([Direction.LONG, Direction.SHORT]),
                round(rng.uniform(1, 50), 2),
                round(rng.uniform(50, 150), 2),
            )
            for _ in range(rng.randint(1, 6))
        ]
        mark = round(rng.uniform(50, 150), 2)
        truth, reported, position = _truth_and_report(fills, mark)

        assert truth == pytest.approx(reported, abs=1e-6), (
            f"fills={fills} mark={mark} qty={position.quantity} avg={position.average_price}"
        )


# --- the side that had no coverage at all --------------------------------


def test_a_short_averages_up_and_realizes_correctly():
    manager = PositionManager()
    manager.apply_fill("acct", "SYM", Direction.SHORT, 10, 100.0)
    manager.apply_fill("acct", "SYM", Direction.SHORT, 10, 110.0)
    position = manager.get("acct", "SYM")

    # Sold 20 for 2100 in total: the average is 105, and the sign of the
    # quantity must not leak into it.
    assert position.quantity == -20.0
    assert position.average_price == pytest.approx(105.0)

    # Buying back below the average is a gain for a short.
    manager.apply_fill("acct", "SYM", Direction.LONG, 20, 95.0)
    assert position.realized_pnl == pytest.approx(20 * (105.0 - 95.0))
    assert position.quantity == 0.0
    assert position.average_price == 0.0


def test_a_short_marks_to_market_in_the_right_direction():
    manager = PositionManager()
    manager.apply_fill("acct", "SYM", Direction.SHORT, 10, 100.0)

    assert manager.mark_to_market("acct", "SYM", 90.0).unrealized_pnl == pytest.approx(100.0)
    assert manager.mark_to_market("acct", "SYM", 110.0).unrealized_pnl == pytest.approx(-100.0)


def test_a_flip_realizes_the_old_side_and_reprices_the_new_one():
    # The defensive branch no production caller can currently reach (see
    # the module docstring), exercised directly.
    manager = PositionManager()
    manager.apply_fill("acct", "SYM", Direction.LONG, 10, 100.0)
    manager.apply_fill("acct", "SYM", Direction.SHORT, 30, 110.0)
    position = manager.get("acct", "SYM")

    assert position.realized_pnl == pytest.approx(10 * (110.0 - 100.0))
    assert position.quantity == -20.0
    assert position.average_price == pytest.approx(110.0), "the new side opens at the fill, not the old average"


# --- control -------------------------------------------------------------


def test_a_losing_trade_reports_a_negative_realized_pnl():
    """Control: losses must stay negative, on both sides.

    Two earlier versions of this control were **vacuous** and injection
    caught both. The first asserted `unrealized_pnl == 0.0` on a flat
    position; the second, that marking a flat position leaves
    `realized_pnl` alone. Neither can fail: a flat position has quantity 0,
    so `(price - average) * 0` is 0 whatever else is wrong, and
    `mark_to_market` returns early for it anyway.

    What can fail is the sign. An `abs()` slipping into the realized
    calculation would make every trade look profitable, and `daily_pnl`,
    the loss halts and `Trade.pnl` all read that number.
    """
    manager = PositionManager()

    manager.apply_fill("acct", "LONG_LOSS", Direction.LONG, 10, 100.0)
    manager.apply_fill("acct", "LONG_LOSS", Direction.SHORT, 10, 90.0)
    assert manager.get("acct", "LONG_LOSS").realized_pnl == pytest.approx(-100.0)

    manager.apply_fill("acct", "SHORT_LOSS", Direction.SHORT, 10, 100.0)
    manager.apply_fill("acct", "SHORT_LOSS", Direction.LONG, 10, 110.0)
    assert manager.get("acct", "SHORT_LOSS").realized_pnl == pytest.approx(-100.0)
