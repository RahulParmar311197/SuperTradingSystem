"""Configurable trading-cost model (blueprint §47)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CostModel:
    brokerage_flat: float = 0.0
    brokerage_pct: float = 0.0
    slippage_pct: float = 0.0
    spread_pct: float = 0.0
    taxes_pct: float = 0.0
    contract_charges_flat: float = 0.0

    def entry_price(self, raw_price: float, is_long: bool) -> float:
        slip = raw_price * (self.slippage_pct + self.spread_pct / 2) / 100
        return raw_price + slip if is_long else raw_price - slip

    def exit_price(self, raw_price: float, is_long: bool) -> float:
        slip = raw_price * (self.slippage_pct + self.spread_pct / 2) / 100
        return raw_price - slip if is_long else raw_price + slip

    def round_trip_costs(self, notional_entry: float, notional_exit: float) -> float:
        pct_costs = (notional_entry + notional_exit) * (self.brokerage_pct + self.taxes_pct) / 100
        return pct_costs + 2 * self.brokerage_flat + self.contract_charges_flat
