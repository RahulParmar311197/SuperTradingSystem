"""Position tracking (blueprint §60): quantity, average price, realized and
unrealized P&L, stop/target."""

from __future__ import annotations

from dataclasses import dataclass

from app.database.models.strategy import Direction


@dataclass(slots=True)
class PositionRecord:
    account_id: str
    symbol: str
    quantity: float = 0.0  # signed: positive = long, negative = short
    average_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    stop: float | None = None
    target: float | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0


class PositionManager:
    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], PositionRecord] = {}

    def get(self, account_id: str, symbol: str) -> PositionRecord | None:
        return self._positions.get((account_id, symbol))

    def apply_fill(
        self, account_id: str, symbol: str, direction: Direction, quantity: float, price: float
    ) -> PositionRecord:
        key = (account_id, symbol)
        position = self._positions.get(key)
        if position is None:
            position = PositionRecord(account_id=account_id, symbol=symbol)
            self._positions[key] = position

        signed_qty = quantity if direction == Direction.LONG else -quantity
        same_direction = position.quantity == 0 or (position.quantity > 0) == (signed_qty > 0)

        if same_direction:
            new_quantity = position.quantity + signed_qty
            total_cost = position.average_price * position.quantity + price * signed_qty
            position.average_price = total_cost / new_quantity if new_quantity else 0.0
            position.quantity = new_quantity
        else:
            closing_qty = min(abs(signed_qty), abs(position.quantity))
            realized = closing_qty * (price - position.average_price) * (1 if position.quantity > 0 else -1)
            position.realized_pnl += realized

            remainder = abs(signed_qty) - closing_qty
            new_quantity = position.quantity + signed_qty
            position.quantity = new_quantity
            if remainder > 0:
                # position flipped direction; the remainder opens a fresh position at this price
                position.average_price = price
            elif new_quantity == 0:
                position.average_price = 0.0

        return position

    def mark_to_market(self, account_id: str, symbol: str, price: float) -> PositionRecord | None:
        position = self._positions.get((account_id, symbol))
        if position is None or not position.is_open:
            return position
        position.unrealized_pnl = (price - position.average_price) * position.quantity
        return position

    def close(self, account_id: str, symbol: str) -> None:
        self._positions.pop((account_id, symbol), None)

    def open_positions(self, account_id: str) -> list[PositionRecord]:
        return [p for (acct, _), p in self._positions.items() if acct == account_id and p.is_open]
