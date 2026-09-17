"""add instruments.broker_instrument_key

Market-data providers do not accept plain trading symbols. Upstox names
instruments as "NSE_EQ|INE009A01021" or "NSE_INDEX|Nifty 50"; without a
column to hold that, no provider call can name an instrument at all.

Nullable: provider-specific, populated from a downloaded instrument
master, and absent for every row until one is loaded.

Revision ID: e7b41c2a9f08
Revises: d1f3a7c9b204
Create Date: 2026-09-17 04:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b41c2a9f08'
down_revision: Union[str, None] = 'd1f3a7c9b204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instruments', sa.Column('broker_instrument_key', sa.String(length=64), nullable=True))
    op.create_index(
        op.f('ix_instruments_broker_instrument_key'), 'instruments', ['broker_instrument_key'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_instruments_broker_instrument_key'), table_name='instruments')
    op.drop_column('instruments', 'broker_instrument_key')
