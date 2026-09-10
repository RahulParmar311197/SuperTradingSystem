"""make instruments.symbol unique

Revision ID: b7c1d40e9a52
Revises: e88fa1d3295b
Create Date: 2026-09-10 09:40:00.000000

Every reader of `instruments` looks a row up by symbol alone and calls
`.scalar_one_or_none()`, so exactly one row per symbol was already the
contract -- it simply was not enforced, and `POST /instruments` had no
existence check either. A second row for one symbol turned `POST /orders`,
`POST /options/execute`, `compute_correlated_exposure` and the portfolio
snapshot into 500s for every user of that symbol.

If this migration fails with a unique-violation, the database already
contains duplicate symbols and they must be resolved by hand first. That
is deliberate: duplicate instrument rows can each own `candles`, `orders`,
`positions` and `trades` via `instrument_id`, and choosing which row
survives (and what happens to the other's children) is an operator
decision, not something a schema migration should make silently.

    SELECT symbol, count(*), array_agg(id)
    FROM instruments GROUP BY symbol HAVING count(*) > 1;
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b7c1d40e9a52'
down_revision: Union[str, None] = 'e88fa1d3295b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_instruments_symbol'), table_name='instruments')
    op.create_index(op.f('ix_instruments_symbol'), 'instruments', ['symbol'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_instruments_symbol'), table_name='instruments')
    op.create_index(op.f('ix_instruments_symbol'), 'instruments', ['symbol'], unique=False)
