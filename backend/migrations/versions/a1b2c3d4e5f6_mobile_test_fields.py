"""mobile test fields (app build / bundle id / device)

Revision ID: a1b2c3d4e5f6
Revises: 89fd622b1775
Create Date: 2026-07-24 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '89fd622b1775'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('test', schema=None) as batch_op:
        batch_op.add_column(sa.Column('app_build_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('app_bundle_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('device', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('test', schema=None) as batch_op:
        batch_op.drop_column('device')
        batch_op.drop_column('app_bundle_id')
        batch_op.drop_column('app_build_ref')
