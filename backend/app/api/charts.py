import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database.models.users import User
from app.database.session import get_db
from app.ict.engine import ICTConfig, ICTEngine
from app.market.repository import get_candles
from app.smc.engine import SMCConfig, SMCEngine

router = APIRouter(prefix="/charts", tags=["charts"])


def _serialize_smc(context) -> dict:
    return {
        "bias": context.bias,
        "current_zone": context.current_zone,
        "dealing_range": (
            {"high": context.dealing_range.range_high, "low": context.dealing_range.range_low}
            if context.dealing_range
            else None
        ),
        "structure_events": [
            {
                "type": e.event_type.value,
                "direction": e.direction.value,
                "timestamp": e.timestamp.isoformat(),
                "broken_price": e.broken_price,
                "break_price": e.break_price,
            }
            for e in context.structure_events
        ],
        "fair_value_gaps": [
            {
                "direction": g.direction.value,
                "top": g.top,
                "bottom": g.bottom,
                "created_at": g.created_at.isoformat(),
                "mitigated": g.mitigated,
                "filled_percentage": g.filled_percentage,
            }
            for g in context.fair_value_gaps
        ],
        "order_blocks": [
            {
                "direction": b.direction.value,
                "top": b.top,
                "bottom": b.bottom,
                "created_at": b.created_at.isoformat(),
                "strength": b.strength,
                "mitigated": b.mitigated,
            }
            for b in context.order_blocks
        ],
        "liquidity_pools": [
            {
                "side": p.side.value,
                "source": p.source_type.value,
                "price": p.price,
                "swept": p.swept,
                "rejected": p.rejected,
            }
            for p in context.liquidity_pools
        ],
    }


def _serialize_ict(context) -> dict:
    return {
        "current_kill_zones": context.current_kill_zones,
        "opening_range": (
            {
                "date": context.current_opening_range.session_date.isoformat(),
                "high": context.current_opening_range.high,
                "low": context.current_opening_range.low,
            }
            if context.current_opening_range
            else None
        ),
    }


@router.get("/{instrument_id}/smc")
async def get_smc_overlay(
    instrument_id: uuid.UUID,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    swing_length: int = 3,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    candles = await get_candles(db, instrument_id, timeframe, start, end)
    context = SMCEngine(SMCConfig(swing_length=swing_length)).analyze(candles)
    return _serialize_smc(context)


@router.get("/{instrument_id}/ict")
async def get_ict_overlay(
    instrument_id: uuid.UUID,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    candles = await get_candles(db, instrument_id, timeframe, start, end)
    context = ICTEngine(ICTConfig()).analyze(candles)
    return _serialize_ict(context)
