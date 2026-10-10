"""testscript.stats (probation agreement, fast passes, misses)

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('testscript', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stats', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('testscript', schema=None) as batch_op:
        batch_op.drop_column('stats')
