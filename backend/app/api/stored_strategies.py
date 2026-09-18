"""Loading a strategy definition back out of the `strategies` table.

`strategies.definition` is a JSON column written by whatever version of
the DSL was current when the row was saved, and re-validated against
whatever version of the DSL is current when it is read. Those are not the
same thing, and every validator this codebase has added to
`app/strategy/dsl.py` widened the gap: a row written before
`_reject_unfed_condition_types` (round 88), before
`_reject_premium_discount_without_a_zone` (round 113), or before the
`lookback` floor, is stored data that no longer validates.

Six API routes read one of those rows and called
`StrategyDefinition.model_validate` on it bare. Measured by planting a
definition that trips round 88's rule and posting a backtest for it:

    POST /backtest -> pydantic_core.ValidationError, uncaught,
                      app/api/backtest.py:132

which the app's catch-all turns into a 500 with a traceback — for stored
data that is simply out of date, on the endpoint whose whole job is to
tell the author whether a strategy is any good.

`ScannerWorker` and `AutoTradeSupervisor` already get this right: both
wrap the same call per strategy in `try/except`, log "Strategy %s has an
invalid definition; skipping", and carry on with everyone else's
strategies. This gives the API routes the same treatment, with the
message pointed at the person who can actually fix it.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from pydantic import ValidationError

from app.database.models.strategy import Strategy as StrategyRow
from app.strategy.dsl import StrategyDefinition


def parse_stored_definition(strategy_row: StrategyRow) -> StrategyDefinition:
    """The stored definition, or a 422 explaining what is wrong with it.

    422 rather than 500 because the request is well-formed and the server
    is healthy: the stored strategy is what is unusable, and the answer
    has to say so in a way the author can act on. Rather than 200 with an
    empty result, because a strategy that cannot be loaded has not been
    evaluated, and reporting "no signals" for it would be the same
    fabricated claim several earlier rounds removed from the risk paths.
    """
    try:
        return StrategyDefinition.model_validate(strategy_row.definition)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Strategy {strategy_row.id} ({strategy_row.name!r}) is stored with a definition that "
            f"is no longer valid, so it cannot be evaluated. Edit and re-save it to fix: {reasons}",
        ) from exc
