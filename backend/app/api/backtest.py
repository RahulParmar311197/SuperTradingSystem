import dataclasses
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.backtest.cost_model import CostModel
from app.backtest.engine import BacktestEngine
from app.backtest.metrics import BacktestMetricsResult, compute_metrics
from app.backtest.validation import validate_out_of_sample
from app.database.models.backtest import Backtest as BacktestRow
from app.database.models.backtest import BacktestMetrics as BacktestMetricsRow
from app.database.models.backtest import BacktestStatus
from app.database.models.backtest import BacktestTrade as BacktestTradeRow
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.users import User
from app.database.session import get_db
from app.market.repository import get_candles
from app.api.stored_strategies import parse_stored_definition

router = APIRouter(prefix="/backtest", tags=["backtest"])

# See app/api/paper.py: the same ceiling every money field in the API
# carries, bounded by what `Numeric(18, 6)` can hold.
_MAX_MONEY = 1e12


# Each cost is a percentage of notional, and every one of them must make a
# backtest *more* pessimistic, never less. Bounds are REASONED, NOT
# CALIBRATED: above 100% a single round trip costs more than the position
# is worth, which is a typo rather than a cost model, and the flat charges
# are bounded by the same money ceiling every other amount here uses.
#
# `ge=0` is the load-bearing half. `cost_model` used to be a bare `dict`,
# so `{"slippage_pct": -50}` was accepted and meant every fill came in 50%
# better than the market. Measured on one strategy over one set of
# candles: reported net profit went from 8,106 to 224,464 -- a 27x edge
# that exists only in the cost model. A backtest is the artifact someone
# decides to risk money on, and the cost model is the one knob whose whole
# job is to stop it flattering the strategy.
#
# `extra="forbid"` because the dict reached `CostModel(**...)`, a slots
# dataclass, so a typo raised `TypeError` -- which the validate endpoint's
# `except ValueError` does not catch and the run endpoint does not guard at
# all. Measured through both real routes: an unknown key, a string, an
# explicit null and a list each returned **HTTP 500 with a traceback**.
_MAX_COST_PCT = 100.0


class CostModelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    brokerage_flat: float = Field(default=0.0, ge=0, lt=_MAX_MONEY)
    brokerage_pct: float = Field(default=0.0, ge=0, le=_MAX_COST_PCT)
    slippage_pct: float = Field(default=0.0, ge=0, le=_MAX_COST_PCT)
    spread_pct: float = Field(default=0.0, ge=0, le=_MAX_COST_PCT)
    taxes_pct: float = Field(default=0.0, ge=0, le=_MAX_COST_PCT)
    contract_charges_flat: float = Field(default=0.0, ge=0, lt=_MAX_MONEY)

    def to_cost_model(self) -> CostModel:
        return CostModel(**self.model_dump())


class RunBacktestRequest(BaseModel):
    strategy_id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str
    start_date: datetime
    end_date: datetime
    starting_capital: float = Field(default=100_000.0, gt=0, lt=_MAX_MONEY)
    cost_model: CostModelRequest = CostModelRequest()


class OpenPositionResponse(BaseModel):
    """A position the strategy still held when the data ran out.

    Separate from the metrics below on purpose. `total_trades` and every
    number beside it are computed over *closed* trades, and folding an
    unclosed one in would mean marking it to market and calling that a
    result -- a decision about what a backtest reports, not one to take
    silently. What was wrong was that this position was dropped entirely,
    so a run that opened one and held it came back as `total_trades: 0`,
    indistinguishable from a strategy that never fired.
    """

    direction: str
    entry_price: float
    stop: float
    target: float
    quantity: float
    opened_at: datetime
    last_price: float
    # Gross and marked at the final candle's close: no exit happened, so
    # no exit cost is charged and no fill price is being claimed.
    unrealized_pnl: float


class BacktestMetricsResponse(BaseModel):
    backtest_id: uuid.UUID
    total_return: float
    net_profit: float
    win_rate: float
    profit_factor: float | None
    expectancy: float | None
    max_drawdown: float
    sharpe: float | None
    sortino: float | None
    average_win: float | None
    average_loss: float | None
    average_r: float | None
    total_trades: int
    long_trades: int
    short_trades: int
    equity_curve: list[float]
    drawdown_curve: list[float]
    monthly_returns: dict[str, float]
    # None when the run ended flat, which is most of them.
    open_position: OpenPositionResponse | None = None


@router.post("", response_model=BacktestMetricsResponse)
async def run_backtest(
    payload: RunBacktestRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BacktestMetricsResponse:
    strategy_row = await db.get(StrategyRow, payload.strategy_id)
    if strategy_row is None or strategy_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    strategy = parse_stored_definition(strategy_row)

    candles = await get_candles(db, payload.instrument_id, payload.timeframe, payload.start_date, payload.end_date)
    if len(candles) < 10:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Not enough historical candles for a meaningful backtest")

    engine = BacktestEngine(strategy, starting_capital=payload.starting_capital, cost_model=payload.cost_model.to_cost_model())
    trades = engine.run(candles, symbol=str(payload.instrument_id))
    metrics = compute_metrics(trades, payload.starting_capital)
    open_position = (
        OpenPositionResponse(**dataclasses.asdict(engine.open_trade)) if engine.open_trade is not None else None
    )

    backtest_row = BacktestRow(
        user_id=user.id,
        strategy_id=payload.strategy_id,
        instrument_id=payload.instrument_id,
        timeframe=payload.timeframe,
        start_date=payload.start_date,
        end_date=payload.end_date,
        starting_capital=payload.starting_capital,
        cost_model=payload.cost_model.model_dump(),
        status=BacktestStatus.COMPLETED,
        open_position=(open_position.model_dump(mode="json") if open_position is not None else None),
    )
    db.add(backtest_row)
    await db.flush()

    for trade in trades:
        db.add(
            BacktestTradeRow(
                backtest_id=backtest_row.id,
                direction=trade.direction,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                quantity=trade.quantity,
                pnl=trade.pnl,
                r_multiple=trade.r_multiple,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
            )
        )

    db.add(
        BacktestMetricsRow(
            backtest_id=backtest_row.id,
            total_return=metrics.total_return,
            net_profit=metrics.net_profit,
            win_rate=metrics.win_rate,
            profit_factor=metrics.profit_factor,
            expectancy=metrics.expectancy,
            max_drawdown=metrics.max_drawdown,
            sharpe=metrics.sharpe,
            sortino=metrics.sortino,
            average_win=metrics.average_win,
            average_loss=metrics.average_loss,
            average_r=metrics.average_r,
            total_trades=metrics.total_trades,
            long_trades=metrics.long_trades,
            short_trades=metrics.short_trades,
            equity_curve=metrics.equity_curve,
            drawdown_curve=metrics.drawdown_curve,
            monthly_returns=metrics.monthly_returns,
        )
    )
    await db.commit()

    # BacktestMetricsResult is `@dataclass(slots=True)` — no `__dict__`
    # attribute; POST /backtest had never had a test hit it, so it 500'd
    # on every call that reached this line.
    return BacktestMetricsResponse(
        backtest_id=backtest_row.id, open_position=open_position, **dataclasses.asdict(metrics)
    )


class ValidateBacktestRequest(BaseModel):
    strategy_id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str
    start_date: datetime
    end_date: datetime
    starting_capital: float = Field(default=100_000.0, gt=0, lt=_MAX_MONEY)
    cost_model: CostModelRequest = CostModelRequest()
    train_pct: float = 0.6
    validation_pct: float = 0.2


class SplitMetricsResponse(BaseModel):
    total_return: float
    net_profit: float
    win_rate: float
    profit_factor: float | None
    expectancy: float | None
    max_drawdown: float
    sharpe: float | None
    sortino: float | None
    total_trades: int


def _to_split_response(m: BacktestMetricsResult) -> SplitMetricsResponse:
    return SplitMetricsResponse(
        total_return=m.total_return,
        net_profit=m.net_profit,
        win_rate=m.win_rate,
        profit_factor=m.profit_factor,
        expectancy=m.expectancy,
        max_drawdown=m.max_drawdown,
        sharpe=m.sharpe,
        sortino=m.sortino,
        total_trades=m.total_trades,
    )


class OutOfSampleResponse(BaseModel):
    train: SplitMetricsResponse
    validation: SplitMetricsResponse
    test: SplitMetricsResponse
    consistent: bool
    warnings: list[str]


@router.post("/validate", response_model=OutOfSampleResponse)
async def validate_backtest(
    payload: ValidateBacktestRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> OutOfSampleResponse:
    """Out-of-sample validation (blueprint §77-78): runs the strategy
    independently over train/validation/test splits of the same history
    rather than one pass over the whole thing, and flags the simple
    overfitting smells. Nothing here is persisted — it's an analysis step
    you run before ever marking a strategy `eligible_for_auto_trading`."""
    strategy_row = await db.get(StrategyRow, payload.strategy_id)
    if strategy_row is None or strategy_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    strategy = parse_stored_definition(strategy_row)

    candles = await get_candles(db, payload.instrument_id, payload.timeframe, payload.start_date, payload.end_date)
    if len(candles) < 30:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Not enough historical candles to split into train/validation/test"
        )

    try:
        report = validate_out_of_sample(
            strategy,
            candles,
            symbol=str(payload.instrument_id),
            starting_capital=payload.starting_capital,
            cost_model=payload.cost_model.to_cost_model(),
            train_pct=payload.train_pct,
            validation_pct=payload.validation_pct,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return OutOfSampleResponse(
        train=_to_split_response(report.train),
        validation=_to_split_response(report.validation),
        test=_to_split_response(report.test),
        consistent=report.consistent,
        warnings=report.warnings,
    )


@router.get("/{backtest_id}", response_model=BacktestMetricsResponse)
async def get_backtest(
    backtest_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BacktestMetricsResponse:
    backtest_row = await db.get(BacktestRow, backtest_id)
    if backtest_row is None or backtest_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backtest not found")

    metrics_row = (
        await db.execute(select(BacktestMetricsRow).where(BacktestMetricsRow.backtest_id == backtest_id))
    ).scalar_one_or_none()
    if metrics_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backtest metrics not found")

    return BacktestMetricsResponse(
        backtest_id=backtest_id,
        total_return=metrics_row.total_return,
        net_profit=metrics_row.net_profit,
        win_rate=metrics_row.win_rate,
        profit_factor=metrics_row.profit_factor,
        expectancy=metrics_row.expectancy,
        max_drawdown=metrics_row.max_drawdown,
        sharpe=metrics_row.sharpe,
        sortino=metrics_row.sortino,
        average_win=metrics_row.average_win,
        average_loss=metrics_row.average_loss,
        average_r=metrics_row.average_r,
        total_trades=metrics_row.total_trades,
        long_trades=metrics_row.long_trades,
        short_trades=metrics_row.short_trades,
        equity_curve=metrics_row.equity_curve,
        drawdown_curve=metrics_row.drawdown_curve,
        monthly_returns=metrics_row.monthly_returns,
        open_position=(
            OpenPositionResponse(**backtest_row.open_position)
            if backtest_row.open_position is not None
            else None
        ),
    )
