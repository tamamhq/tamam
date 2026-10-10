"""project.org_id (tenant owner)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add nullable, backfill existing rows to the dev org, then enforce NOT NULL.
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('org_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.execute("UPDATE project SET org_id = 'org_dev' WHERE org_id IS NULL")
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.alter_column('org_id', nullable=False)
        batch_op.create_index('ix_project_org_id', ['org_id'])


def downgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_index('ix_project_org_id')
        batch_op.drop_column('org_id')
