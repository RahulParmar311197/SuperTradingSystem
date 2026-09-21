"""Three of the four execution paths never reached the risk metrics.

`RISK_REJECTION_COUNT` is declared as "Total orders rejected by the risk
engine" and `ORDER_COUNT` as "Total orders submitted". Both are
process-wide counters with no user label: they answer "is this
deployment's trading healthy", and a spike in rejections is how an
operator learns an account has stopped trading -- the daily breaker
tripped, exposure is exhausted, the kill switch is engaged.

Only `POST /orders` incremented the rejection counter. Measured on one
account at one instant, both refusals from the same engine with the same
reason:

    POST /orders          -> 403 "Daily loss 12.50% vs limit 2.0%"
    POST /options/execute -> 403 "Daily loss 12.50% vs limit 2.0%"

    risk_rejections_total: 0.0 -> 1.0 (equity) -> 1.0 (options, +0.0)

And on the autonomous path, with a cap of one trade a day and two
instruments -- the supervisor opened a real position and had a second
entry refused, writing RiskEvent rows for both:

    orders_total          : 0.0 -> 0.0 (fill) -> 0.0 (rejection)
    risk_rejections_total : 0.0 -> 0.0 (fill) -> 0.0 (rejection)

So the one execution path with no human watching it was also the one
invisible to monitoring.

METHOD NOTE. Round 162 swept `/orders` against `/options/execute` and
fixed `ORDER_COUNT` there; paper and auto were never in that sweep's
scope, which is why this survived it. The structural test at the bottom
runs the check over all four paths at once, so a fifth path cannot be
added without one.

SCOPE. The `/paper` sandbox is deliberately NOT counted --
`test_the_paper_sandbox_is_deliberately_not_counted` pins that as a
decision rather than an omission, with the reasoning at the call site in
app/api/paper.py.
"""

import ast
import pathlib
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.core.metrics import ORDER_COUNT, RISK_REJECTION_COUNT
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.market import Candle as CandleRow
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion
from app.database.models.trading import Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle
from app.workers.auto_trade_worker import AutoTradeSupervisor

# --- reading the counters -------------------------------------------------
#
# These are process-global and every other test in the suite moves them,
# so every assertion here is a DELTA around one action, never an absolute.


def _rejections() -> float:
    return RISK_REJECTION_COUNT._value.get()


def _orders(status: str) -> float:
    return ORDER_COUNT.labels(status)._value.get()


def _orders_all() -> float:
    total = 0.0
    for metric in ORDER_COUNT.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
    return total


# --- the equity/options harness ------------------------------------------

async def _inst(**kw) -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(exchange="NSE", active=True, **kw)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _equity() -> Instrument:
    return await _inst(symbol=f"MP{uuid.uuid4().hex[:8].upper()}", market=MarketType.EQUITY, instrument_type="EQ")


async def _contract(strike: float) -> Instrument:
    return await _inst(
        symbol=f"MQ{uuid.uuid4().hex[:8].upper()}", market=MarketType.OPTIONS, instrument_type="OPT",
        option_type=OptionType.CALL, strike=strike, lot_size=50,
        expiry=date.today() + timedelta(days=21),
    )


async def _user(client) -> tuple[uuid.UUID, dict]:
    email = f"mp-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(
            id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"),
            name="Metrics Parity", trading_permissions=[TradingPermission.LIVE_TRADE.value],
        )
        db.add(row)
        await db.commit()
        user_id = row.id
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    return user_id, {"Authorization": f"Bearer {token}"}


def _spread(a: Instrument, b: Instrument, *, quantity: int = 1) -> dict:
    return {
        "strategy_name": f"Bull call spread {uuid.uuid4().hex[:6]}",
        "legs": [
            {"symbol": a.symbol, "direction": "LONG", "quantity": quantity, "premium": 5.0},
            {"symbol": b.symbol, "direction": "SHORT", "quantity": quantity, "premium": 2.0},
        ],
    }


def _blow_the_daily_limit(client, headers, equity: Instrument) -> None:
    """Open and close far below, realizing a loss past the 2% daily cap."""
    client.post("/orders", headers=headers, json={
        "symbol": equity.symbol, "direction": "LONG", "order_type": "MARKET", "entry": 100.0, "stop": 99.0})
    client.post("/orders", headers=headers, json={
        "symbol": equity.symbol, "direction": "SHORT", "order_type": "MARKET", "entry": 75.0, "stop": 76.0})


async def _cleanup(user_id: uuid.UUID, instruments: list[Instrument]) -> None:
    from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS

    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        # Child rows first: `strategy_versions` references `strategies`
        # (POST /strategies snapshots a version on create).
        strategy_ids = (await db.execute(
            select(StrategyRow.id).where(StrategyRow.user_id == user_id))).scalars().all()
        if strategy_ids:
            await db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id.in_(strategy_ids)))
        for model in (Notification, RiskEvent, AuditLog, UserSession, StrategyRow):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        for row in instruments:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == row.id))
            await db.execute(delete(Instrument).where(Instrument.id == row.id))
        await db.commit()
    for cache in (_STACKS, _STACK_LOCKS, _TRADE_LOCKS):
        cache.pop(user_id, None)


# --- the autonomous harness ----------------------------------------------
#
# The same bullish sweep+FVG dataset every other auto-trade test uses: it
# matches on bar 8 and runs to target on bar 9.

SETUP = [
    (100, 100, 99, 100), (100, 102, 100, 101), (101, 103, 100, 102), (102, 102, 97, 98),
    (98, 99, 96, 97), (97, 100, 96, 99), (99, 108, 99, 107), (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces into the FVG -> entry
    (104, 130, 104, 128),  # runs to target -> close
]

STRATEGY_DEFINITION = {
    "name": "Bullish FVG retest",
    "market": "TESTSYM",
    "timeframe": "15m",
    "direction": "bullish",
    "conditions": [{"type": "fvg", "direction": "bullish"}],
    "entry": {"type": "fvg_retest"},
    "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
}

# The equity liquidity gate caps an order at a share of the bar's traded
# volume, and these setups size to hundreds of shares.
LIQUID_BAR_VOLUME = 50_000.0


def _candles() -> list[Candle]:
    """Anchored recently so the supervisor's freshness gate does not refuse
    the series. Same reasoning as the sibling worker tests."""
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)
    return [Candle(start + timedelta(minutes=i), o, h, l, c, LIQUID_BAR_VOLUME)
            for i, (o, h, l, c) in enumerate(SETUP)]


async def _auto_user(markets: list[str], *, max_trades_per_day: int = 1) -> uuid.UUID:
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(), email=f"ma-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"), name="Metrics Auto",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True, auto_trading_risk_per_trade_pct=1.0,
            auto_trading_max_positions=5, auto_trading_max_trades_per_day=max_trades_per_day,
            auto_trading_daily_loss_limit_pct=2.0,
        )
        db.add(user)
        await db.flush()
        for market in markets:
            db.add(StrategyRow(
                user_id=user.id, name=f"Bullish FVG retest {market}",
                definition={**STRATEGY_DEFINITION, "market": market},
                is_active=True, eligible_for_auto_trading=True,
            ))
        await db.commit()
        return user.id


async def _feed(supervisor: AutoTradeSupervisor, instrument_id: uuid.UUID, candles: list[Candle], bars: int) -> None:
    for i in range(bars):
        async with async_session_factory() as db:
            await upsert_candles(db, instrument_id, "15m", [candles[i]])
        await supervisor.run_once()


async def _capped_rejections(user_id: uuid.UUID) -> list[RiskEvent]:
    async with async_session_factory() as db:
        events = (await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))).scalars().all()
    return [e for e in events if e.checks is not None and e.checks.get("max_trades_per_day") is False]


# --- the finding ----------------------------------------------------------


async def test_an_options_rejection_reaches_the_rejection_counter(require_infra):
    """The headline, through the real endpoint."""
    equity, a, b = await _equity(), await _contract(100.0), await _contract(110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            _blow_the_daily_limit(client, headers, equity)

            before = _rejections()
            r = client.post("/options/execute", headers=headers, json=_spread(a, b))
            after = _rejections()

            assert r.status_code == 403, r.text
            assert "Daily loss" in r.json()["detail"]
            # The refusal itself was never in doubt -- what was missing is
            # that it reached the metric an operator alerts on.
            assert after - before == 1.0, "an options refusal must count as a risk rejection"
        finally:
            await _cleanup(user_id, [equity, a, b])


async def test_an_approved_options_execution_does_not_count_a_rejection(require_infra):
    """Control. An over-fix that increments on every call to this endpoint,
    rather than only on the rejection branch, passes the test above and
    fails this one."""
    a, b = await _contract(100.0), await _contract(110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            before = _rejections()
            r = client.post("/options/execute", headers=headers, json=_spread(a, b))
            after = _rejections()

            assert r.status_code == 201, r.text
            assert after - before == 0.0, "an approved strategy is not a risk rejection"
        finally:
            await _cleanup(user_id, [a, b])


async def test_an_autonomous_rejection_reaches_the_rejection_counter(require_infra):
    """The unattended path, driven through the real AutoTradeSupervisor.

    A cap of one trade a day and two instruments: the first fills, the
    second is refused by the risk engine.
    """
    a, b = await _equity(), await _equity()
    user_id = await _auto_user([a.symbol, b.symbol], max_trades_per_day=1)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        candles = _candles()

        await _feed(supervisor, a.id, candles, 9)

        before = _rejections()
        await _feed(supervisor, b.id, candles, 9)
        after = _rejections()

        capped = await _capped_rejections(user_id)
        # Non-vacuity: the run must really have produced refusals naming
        # the cap, or this test would pass on a supervisor that did nothing.
        assert capped, "the second instrument must have been refused by max_trades_per_day"
        assert after - before == float(len(capped)), (
            "every autonomous risk rejection must reach the counter, one for one"
        )
    finally:
        await _cleanup(user_id, [a, b])


async def test_an_autonomous_fill_reaches_the_order_counter(require_infra):
    """The same blind spot on the fill side: `orders_total` counted manual
    and options orders and no autonomous one."""
    a = await _equity()
    user_id = await _auto_user([a.symbol])
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")

        # MONITORING, not FILLED: the execution engine transitions a filled
        # entry to MONITORING once the position is open, so that is the
        # bucket `POST /orders` lands in too. Asserting it pins the parity
        # rather than just "some counter moved".
        before_all, before_open = _orders_all(), _orders("MONITORING")
        await _feed(supervisor, a.id, _candles(), 9)
        after_all, after_open = _orders_all(), _orders("MONITORING")

        async with async_session_factory() as db:
            positions = (await db.execute(select(Position).where(Position.user_id == user_id))).scalars().all()
        # Non-vacuity: a run that opened nothing would trivially satisfy a
        # "counter did not move" reading of this.
        assert len(positions) == 1, "the supervisor must have opened exactly one position"

        assert after_all - before_all == 1.0, "an autonomous fill must count as an order"
        # Labelled by the status the order really reached, not a constant --
        # anything else puts autonomous fills in a bucket the other two
        # paths never use, which is the same invisibility in a new place.
        assert after_open - before_open == 1.0, "and under the real order status"
    finally:
        await _cleanup(user_id, [a])


async def test_the_paper_sandbox_is_deliberately_not_counted(require_infra, monkeypatch):
    """A DECISION, not an omission -- see the reasoning at the call site in
    app/api/paper.py. These counters are process-wide with no user label;
    a user hand-feeding a long candle series into a private sandbox would
    drown the signal they exist to carry.
    """
    from app.risk.engine import RiskEngine
    from app.risk.limits import RiskDecision, RiskDecisionResult

    monkeypatch.setattr(
        RiskEngine, "evaluate",
        lambda self, proposal: RiskDecisionResult(RiskDecision.REJECT, [], "forced rejection for test"),
    )

    instrument = await _equity()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            created = client.post("/strategies", headers=headers, json={
                **STRATEGY_DEFINITION, "name": f"Sandbox {uuid.uuid4().hex[:6]}",
                "market": instrument.symbol})
            assert created.status_code in (200, 201), created.text
            strategy_id = created.json()["id"]

            session = client.post("/paper", headers=headers, json={
                "strategy_id": strategy_id, "symbol": instrument.symbol, "timeframe": "15m"})
            assert session.status_code == 200, session.text
            session_id = session.json()["session_id"]

            before_rej, before_ord = _rejections(), _orders_all()
            for candle in _candles():
                client.post(f"/paper/{session_id}/candle", headers=headers, json={
                    "timestamp": candle.timestamp.isoformat(), "open": candle.open, "high": candle.high,
                    "low": candle.low, "close": candle.close, "volume": candle.volume})
            after_rej, after_ord = _rejections(), _orders_all()

            async with async_session_factory() as db:
                events = (await db.execute(
                    select(RiskEvent).where(RiskEvent.user_id == user_id))).scalars().all()
            # Non-vacuity: the sandbox must really have reached a risk
            # decision, or "the counters did not move" says nothing at all.
            assert events, "the sandbox must have produced at least one risk decision"
            assert any(e.reason == "forced rejection for test" for e in events)

            assert after_rej - before_rej == 0.0, "the sandbox must not move the account-level counter"
            assert after_ord - before_ord == 0.0
        finally:
            await _cleanup(user_id, [instrument])


def test_every_path_that_audits_a_risk_decision_also_counts_it():
    """Structural, and labelled as such: it asserts the SET of paths.

    Round 162 swept only `/orders` against `/options/execute`, so the
    paper and autonomous paths were outside it and this survived. Running
    the check over all four at once means a fifth execution path cannot
    appear without either counting its rejections or documenting why not.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    paths = {
        "orders": root / "api" / "orders.py",
        "options": root / "api" / "options.py",
        "paper": root / "api" / "paper.py",
        "auto": root / "workers" / "auto_trade_worker.py",
    }

    audits: dict[str, bool] = {}
    counts: dict[str, bool] = {}
    for label, path in paths.items():
        tree = ast.parse(path.read_text())
        audits[label] = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "RiskEvent"
            for n in ast.walk(tree)
        )
        # The CALL, not merely the imported name -- a leftover `from ...
        # import RISK_REJECTION_COUNT` with the call deleted must not
        # satisfy this. (Round 159 shipped exactly that escape.)
        counts[label] = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "inc"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "RISK_REJECTION_COUNT"
            for n in ast.walk(tree)
        )

    assert all(audits.values()), f"every execution path writes a RiskEvent row: {audits}"

    # The one documented exception, with its reasoning at the call site.
    assert counts == {"orders": True, "options": True, "paper": False, "auto": True}, counts

    # And the declension is recorded where a reader will meet it, so it
    # stays a decision rather than decaying into an oversight.
    paper_source = paths["paper"].read_text()
    assert "DELIBERATELY no RISK_REJECTION_COUNT" in paper_source
