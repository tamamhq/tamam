"""clauses + trace_key on testrun (durable evidence for test graduation)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('testrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('clauses', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('trace_key', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('testrun', schema=None) as batch_op:
        batch_op.drop_column('trace_key')
        batch_op.drop_column('clauses')
