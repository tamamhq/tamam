"""builds table + test.build_id

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'build',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('app_path', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('bundle_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('vendor_refs', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['project.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_build_project_id', 'build', ['project_id'])
    with op.batch_alter_table('test', schema=None) as batch_op:
        batch_op.add_column(sa.Column('build_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key('test_build_id_fkey', 'build', ['build_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('test', schema=None) as batch_op:
        batch_op.drop_constraint('test_build_id_fkey', type_='foreignkey')
        batch_op.drop_column('build_id')
    op.drop_index('ix_build_project_id', table_name='build')
    op.drop_table('build')
