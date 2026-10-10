"""graduation: testscript table, project settings, test/run graduation fields

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'testscript',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('test_id', sa.Uuid(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('body', sa.JSON(), nullable=True),
        sa.Column('test_text_hash', sa.String(), nullable=False),
        sa.Column('source_run_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['test_id'], ['test.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('test_id', 'version', name='uq_testscript_version'),
    )
    op.create_index('ix_testscript_test_id', 'testscript', ['test_id'])
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column('settings', sa.JSON(), nullable=True))
    op.execute("UPDATE project SET settings = '{}' WHERE settings IS NULL")
    with op.batch_alter_table('test', schema=None) as batch_op:
        batch_op.add_column(sa.Column('graduation', sa.JSON(), nullable=True))
    with op.batch_alter_table('testrun', schema=None) as batch_op:
        batch_op.add_column(sa.Column('graduation', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('testrun', schema=None) as batch_op:
        batch_op.drop_column('graduation')
    with op.batch_alter_table('test', schema=None) as batch_op:
        batch_op.drop_column('graduation')
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('settings')
    op.drop_index('ix_testscript_test_id', table_name='testscript')
    op.drop_table('testscript')
