"""The daily circuit breaker told two different stories on two paths.

Blueprint §63 wants the daily loss limit distinguishable from an ordinary
rejection, and `POST /orders` has made that distinction since round 65: it
reads the first FAILED check's name and sends DAILY_LOSS_LIMIT rather than
a generic ORDER_REJECTED when that is why. `POST /options/execute` sent
ORDER_REJECTED unconditionally -- though `evaluate_options_risk` produces
the identically-named "daily_loss_limit" check, so the information was
computed on that path and thrown away.

Measured on one account at one moment, past its 2% daily limit:

    equity order     -> 403, notification ORDER_REJECTED? no: DAILY_LOSS_LIMIT
    options strategy -> 403, notification ORDER_REJECTED

Same breaker, same instant, two different stories -- and the breaker is
the one an operator most needs to see, because it means the account is
done for the day.

METHOD NOTE. Round 162 swept `/orders` against `/options/execute` by
diffing every name each path calls, and could not have found this: both
paths call `create_notification`, and the difference is in an ARGUMENT.
That sweep's blind spot is what this file's structural test closes --
it diffs the notification TYPES each path can emit, which is the level at
which this class of bug lives.
"""

import ast
import pathlib
import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification, NotificationType
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.trading.persistence import persist_position
from app.trading.position_manager import PositionRecord


async def _inst(**kw) -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(exchange="NSE", active=True, **kw)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _equity() -> Instrument:
    return await _inst(symbol=f"NP{uuid.uuid4().hex[:8].upper()}", market=MarketType.EQUITY, instrument_type="EQ")


async def _contract(strike: float) -> Instrument:
    return await _inst(
        symbol=f"NC{uuid.uuid4().hex[:8].upper()}", market=MarketType.OPTIONS, instrument_type="OPT",
        option_type=OptionType.CALL, strike=strike, lot_size=50,
        expiry=date.today() + timedelta(days=21),
    )


async def _user(client) -> tuple[uuid.UUID, dict]:
    email = f"np-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(
            id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"),
            name="Notify Parity", trading_permissions=[TradingPermission.LIVE_TRADE.value],
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


async def _types_for(user_id: uuid.UUID, title_contains: str) -> list[NotificationType]:
    async with async_session_factory() as db:
        rows = (await db.execute(
            select(Notification).where(Notification.user_id == user_id).order_by(Notification.created_at.desc())
        )).scalars().all()
    return [r.type for r in rows if title_contains in r.title]


async def _cleanup(user_id: uuid.UUID, instruments: list[Instrument]) -> None:
    from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS

    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        for model in (Notification, RiskEvent, AuditLog, UserSession):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        for row in instruments:
            await db.execute(delete(Instrument).where(Instrument.id == row.id))
        await db.commit()
    for cache in (_STACKS, _STACK_LOCKS, _TRADE_LOCKS):
        cache.pop(user_id, None)


# --- the finding ----------------------------------------------------------


async def test_the_options_path_names_the_daily_loss_limit(require_infra):
    """The headline, through the real endpoint."""
    equity, a, b = await _equity(), await _contract(100.0), await _contract(110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            _blow_the_daily_limit(client, headers, equity)
            r = client.post("/options/execute", headers=headers, json=_spread(a, b))
            assert r.status_code == 403, r.text
            assert "Daily loss" in r.text, "fixture: the daily limit must be the reason"

            types = await _types_for(user_id, "options strategy rejected")
            assert types == [NotificationType.DAILY_LOSS_LIMIT], types
        finally:
            await _cleanup(user_id, [equity, a, b])


async def test_both_paths_tell_the_same_story_about_the_breaker(require_infra):
    """Parity, stated directly: one account, one moment, one verdict."""
    equity, a, b = await _equity(), await _contract(100.0), await _contract(110.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            _blow_the_daily_limit(client, headers, equity)
            client.post("/orders", headers=headers, json={
                "symbol": equity.symbol, "direction": "LONG", "order_type": "MARKET",
                "entry": 100.0, "stop": 99.0})
            client.post("/options/execute", headers=headers, json=_spread(a, b))

            assert await _types_for(user_id, "order rejected") == [NotificationType.DAILY_LOSS_LIMIT]
            assert await _types_for(user_id, "options strategy rejected") == [NotificationType.DAILY_LOSS_LIMIT]
        finally:
            await _cleanup(user_id, [equity, a, b])


# --- what the fix must not break -----------------------------------------


async def test_a_rejection_for_another_reason_is_still_ORDER_REJECTED(require_infra):
    """The control that matters: relabelling every options rejection as the
    breaker would satisfy the proof above. An exposure breach is not the
    daily loss limit and must keep saying so."""
    a, b = await _contract(100.0), await _contract(110.0)
    elsewhere = await _contract(90.0)
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            async with async_session_factory() as db:
                await persist_position(
                    db, user_id, elsewhere.id,
                    PositionRecord(account_id=str(user_id), symbol=elsewhere.symbol, quantity=950.0,
                                   average_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0),
                    ExecutionMode.PAPER, source_key="auto",
                )
                await db.commit()

            r = client.post("/options/execute", headers=headers, json=_spread(a, b, quantity=40))
            assert r.status_code == 403, r.text
            assert "xposure" in r.text, f"fixture: exposure must be the reason, not the breaker: {r.text}"

            types = await _types_for(user_id, "options strategy rejected")
            assert types == [NotificationType.ORDER_REJECTED], types
        finally:
            await _cleanup(user_id, [a, b, elsewhere])


# --- the sweep itself, so the class stays closed -------------------------


def test_every_execution_path_can_still_raise_the_breaker():
    """STRUCTURAL, and the point of this file.

    Rounds 62, 66, 80, 81, 90 each fixed ONE instance of "path X does not
    notify like path Y". Round 162 closed the options-parity class by
    diffing called NAMES -- and could not see this bug, because every path
    calls `create_notification` and the difference was an argument.

    So this diffs the notification TYPES each execution path can emit. A
    path that loses DAILY_LOSS_LIMIT again fails here.

    SL_HIT/TP_HIT are deliberately absent from the two live paths: a live
    stop rests at the broker, and when it fires the position goes missing
    there while still open locally, which `reconcile_positions` surfaces as
    a halt (round 108) rather than as an exit reason those paths can read.
    RECONCILIATION_REQUIRED is absent from `POST /paper` for the plainer
    reason that a sandbox has no broker to reconcile against."""
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    paths = {
        "api/orders.py": root / "api/orders.py",
        "api/options.py": root / "api/options.py",
        "api/paper.py": root / "api/paper.py",
        "workers/auto_trade_worker.py": root / "workers/auto_trade_worker.py",
    }

    def _emitted(path: pathlib.Path) -> set[str]:
        tree = ast.parse(path.read_text())
        return {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "NotificationType"
        }

    emitted = {name: _emitted(p) for name, p in paths.items()}
    missing = sorted(name for name, kinds in emitted.items() if "DAILY_LOSS_LIMIT" not in kinds)
    assert not missing, f"these execution paths cannot name the daily circuit breaker: {missing}"

    for name in ("ORDER_REJECTED", "TRADE_EXECUTED", "POSITION_CLOSED"):
        absent = sorted(p for p, kinds in emitted.items() if name not in kinds)
        assert not absent, f"{name} is missing from: {absent}"
