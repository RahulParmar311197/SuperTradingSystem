import pytest

from app.paper.engine import PaperTradingEngine
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from tests.smc.conftest import make_candles

SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces into the FVG -> entry
    (104, 130, 104, 128),  # runs hard to target
]


def _strategy() -> StrategyDefinition:
    return StrategyDefinition(
        name="Bullish FVG retest",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[Condition(type=ConditionType.FVG, direction="bullish")],
        entry=EntryConfig(type="fvg_retest"),
        risk=RiskConfig(risk_percent=1.0, minimum_rr=2.0),
    )


@pytest.mark.asyncio
async def test_paper_engine_opens_and_closes_trade_through_full_stack():
    candles = make_candles(SETUP)
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM", starting_balance=100_000)

    order_created = False
    closed_pnl = None
    for candle in candles:
        outcome = await engine.on_candle(candle)
        order_created = order_created or outcome.order_created
        if outcome.closed_position_pnl is not None:
            closed_pnl = outcome.closed_position_pnl

    assert order_created is True
    assert closed_pnl is not None
    assert closed_pnl > 0
    assert engine.trades_today == 1

    position = engine.position_manager.get(engine.account_id, "TESTSYM")
    assert position.is_open is False


@pytest.mark.asyncio
async def test_paper_engine_respects_risk_kill_switch():
    from app.risk.kill_switch import KillSwitchState

    candles = make_candles(SETUP)
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM")
    engine.risk_engine.kill_switch = KillSwitchState()
    engine.risk_engine.kill_switch.kill_global()

    saw_rejection = False
    for candle in candles:
        outcome = await engine.on_candle(candle)
        if outcome.risk_rejected_reason is not None:
            saw_rejection = True

    assert saw_rejection is True
    assert engine.trades_today == 0
