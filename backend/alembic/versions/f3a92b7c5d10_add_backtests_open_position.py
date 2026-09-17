"""add backtests.open_position

`backtest_trades` holds only closed trades. A run that opened a position
and held it to the last candle stored nothing at all, so its metrics read
`total_trades: 0` -- indistinguishable from a strategy that never fired,
and biased toward omitting exactly the trades a wide stop keeps open.

Nullable JSON: NULL means the run ended flat, which is most of them.

Revision ID: f3a92b7c5d10
Revises: e7b41c2a9f08
Create Date: 2026-09-17 05:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a92b7c5d10'
down_revision: Union[str, None] = 'e7b41c2a9f08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('backtests', sa.Column('open_position', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('backtests', 'open_position')
