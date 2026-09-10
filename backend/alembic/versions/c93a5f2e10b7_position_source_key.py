"""give positions a source_key so independent engines stop clobbering

Revision ID: c93a5f2e10b7
Revises: b7c1d40e9a52
Create Date: 2026-09-10 10:20:00.000000

`positions` is a DB mirror of an in-memory `PositionManager`, and three
unrelated ones write into it: the manual/live stack in `app/api/orders.py`,
each `PaperTradingEngine` behind `POST /paper`, and `AutoTradeSupervisor`
in the worker process. All of them persist under `ExecutionMode.PAPER`
whenever no broker is connected -- every account's default -- so the old
lookup key (user, instrument, execution_mode, is_open) collided and
whichever engine wrote last silently overwrote the others.

Existing rows are backfilled to 'manual'. There is no way to recover which
engine actually produced a historical row, and the rows are unreliable
anyway precisely because they have been overwriting each other; 'manual'
is chosen because `POST /orders` is the path most likely to have produced
a surviving row. Operators who care can re-derive intent from `trades`,
which has always carried `strategy_id`.

The partial unique index can be created safely on existing data: the old
code could only ever leave one *open* row per (user, instrument,
execution_mode), since a second writer overwrote rather than inserted.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c93a5f2e10b7'
down_revision: Union[str, None] = 'b7c1d40e9a52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'positions',
        sa.Column('source_key', sa.String(length=72), nullable=False, server_default='manual'),
    )
    op.create_index(
        'uq_open_position_per_source',
        'positions',
        ['user_id', 'instrument_id', 'execution_mode', 'source_key'],
        unique=True,
        postgresql_where=sa.text('is_open'),
    )


def downgrade() -> None:
    op.drop_index('uq_open_position_per_source', table_name='positions')
    op.drop_column('positions', 'source_key')
