"""Train/validation/test period splitting to guard against overfitting
(blueprint §78)."""

from __future__ import annotations

from dataclasses import dataclass

from app.smc.types import Candle


@dataclass(slots=True)
class DatasetSplit:
    train: list[Candle]
    validation: list[Candle]
    test: list[Candle]


def split_periods(candles: list[Candle], train_pct: float = 0.6, validation_pct: float = 0.2) -> DatasetSplit:
    if not 0 < train_pct < 1 or not 0 < validation_pct < 1 or train_pct + validation_pct >= 1:
        raise ValueError("train_pct + validation_pct must be < 1, and each must be in (0, 1)")

    n = len(candles)
    train_end = int(n * train_pct)
    validation_end = train_end + int(n * validation_pct)

    return DatasetSplit(
        train=candles[:train_end],
        validation=candles[train_end:validation_end],
        test=candles[validation_end:],
    )
