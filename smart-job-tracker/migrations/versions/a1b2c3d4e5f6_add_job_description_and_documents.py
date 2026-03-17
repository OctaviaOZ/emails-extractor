"""Add job_description to jobapplication and applicationdocument table

Revision ID: a1b2c3d4e5f6
Revises: 3af44128e270
Create Date: 2026-03-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '3af44128e270'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)

    # Add job_description column to jobapplication
    existing_cols = [c['name'] for c in inspector.get_columns('jobapplication')]
    if 'job_description' not in existing_cols:
        op.add_column('jobapplication', sa.Column('job_description', sa.Text(), nullable=True))

    # Create applicationdocument table
    if 'applicationdocument' not in inspector.get_table_names():
        op.create_table(
            'applicationdocument',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('application_id', sa.Integer(), nullable=False),
            sa.Column('filename', sa.String(), nullable=False),
            sa.Column('doc_type', sa.String(), nullable=False, server_default='cv'),
            sa.Column('file_data', sa.LargeBinary(), nullable=False),
            sa.Column('uploaded_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['application_id'], ['jobapplication.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade() -> None:
    op.drop_table('applicationdocument')
    op.drop_column('jobapplication', 'job_description')
