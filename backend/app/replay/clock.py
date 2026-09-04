"""Replay clock (blueprint §41-42, §45).

The single rule that matters here: `visible_candles` never returns more
than `candles[:cursor + 1]`. Every downstream consumer (SMC/ICT/strategy)
must be handed that slice — never the full `candles` list — or the
look-ahead guarantee breaks.
"""

from __future__ import annotations

from enum import StrEnum

from app.smc.types import Candle


class ReplayStatus(StrEnum):
    CREATED = "CREATED"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"


class ReplayClock:
    def __init__(self, candles: list[Candle], speed: float = 1.0) -> None:
        if not candles:
            raise ValueError("Replay requires at least one candle")
        self.candles = candles
        self.cursor = 0
        self.speed = speed
        self.status = ReplayStatus.CREATED

    @property
    def visible_candles(self) -> list[Candle]:
        return self.candles[: self.cursor + 1]

    @property
    def current_candle(self) -> Candle:
        return self.candles[self.cursor]

    @property
    def is_finished(self) -> bool:
        return self.cursor >= len(self.candles) - 1

    def play(self) -> None:
        if self.is_finished:
            self.status = ReplayStatus.FINISHED
            return
        self.status = ReplayStatus.PLAYING

    def pause(self) -> None:
        self.status = ReplayStatus.PAUSED

    def reset(self) -> None:
        self.cursor = 0
        self.status = ReplayStatus.CREATED

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.speed = speed

    def next_candle(self) -> Candle:
        if self.is_finished:
            self.status = ReplayStatus.FINISHED
            return self.current_candle
        self.cursor += 1
        if self.is_finished:
            self.status = ReplayStatus.FINISHED
        return self.current_candle
