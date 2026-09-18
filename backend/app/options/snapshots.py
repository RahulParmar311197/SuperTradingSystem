"""Reading the latest quote for an option instrument (blueprint §40).

One function, in one place, for a reason this codebase has already paid
for once. Both consumers -- `app/api/options.py`'s liquidity / premium /
staleness gates and `app/trading/portfolio_snapshots.py`'s net Greeks --
had their own inlined copy of the same two queries. An earlier round found
the same shape in the correlation code and learned that breaking *both*
copies at once left the whole suite green, so the duplication is what gets
removed here, not just the bug inside it.

The bug: each copy asked for the instrument's `OptionContract` with
`.scalar_one_or_none()`, which raises `MultipleResultsFound` the moment an
instrument has more than one. Nothing enforced one row per instrument, and
an ingestion run creates a fresh `option_chains` row with fresh
`option_contracts` beneath it -- so **the second chain fetch of the day
would have turned `POST /options/execute` into a 500.** Measured directly
against Postgres before the fix:

    two contracts for one instrument -> MultipleResultsFound

That is not a hypothetical: it is what happens on the second run of the
ingestion this round adds, which is why the two ship together.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.options import OptionContract, OptionSnapshot


async def latest_option_snapshot(db: AsyncSession, instrument_id: uuid.UUID) -> OptionSnapshot | None:
    """The newest quote for `instrument_id`, across every chain fetch.

    Joins rather than resolving a contract first, so the answer is the
    latest snapshot the instrument has *anywhere* — which is the question
    both callers were actually asking. Picking one contract and then its
    latest snapshot would return a stale quote whenever the newest fetch
    happened to create a different contract row.
    """
    return (
        await db.execute(
            select(OptionSnapshot)
            .join(OptionContract, OptionSnapshot.option_contract_id == OptionContract.id)
            .where(OptionContract.instrument_id == instrument_id)
            .order_by(OptionSnapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalars().first()
