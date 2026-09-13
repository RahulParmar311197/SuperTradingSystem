"""widen positions.protective_order_id to match orders.broker_order_id

Both columns hold the same kind of value -- an id issued by the broker --
but `orders.broker_order_id` is String(128) and this one was added as
String(64). An id cannot be truncated to fit (a shortened order id
matches nothing at the broker), so the narrower column meant any broker
id between 65 and 128 characters was accepted by the order journal and
rejected by the position journal, raising StringDataRightTruncation
*after* the protective stop had already been placed at the venue.

Widening only; no data can fail to fit.

Revision ID: d1f3a7c9b204
Revises: bae542907d14
Create Date: 2026-09-13 13:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1f3a7c9b204'
down_revision: Union[str, None] = 'bae542907d14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'positions',
        'protective_order_id',
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Narrowing can fail on rows that already hold a longer id; that is
    # the point of the upgrade, and the downgrade does not paper over it.
    op.alter_column(
        'positions',
        'protective_order_id',
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
