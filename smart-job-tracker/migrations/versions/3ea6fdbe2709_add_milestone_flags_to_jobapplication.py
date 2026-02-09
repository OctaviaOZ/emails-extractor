"""add milestone flags to jobapplication

Revision ID: 3ea6fdbe2709
Revises: a96af85729d6
Create Date: 2026-02-07 16:45:55.422074

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '3ea6fdbe2709'
down_revision: Union[str, Sequence[str], None] = 'a96af85729d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add columns
    op.add_column('jobapplication', sa.Column('reached_assessment', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('jobapplication', sa.Column('reached_interview', sa.Boolean(), nullable=False, server_default=sa.text('false')))

    # 2. Backfill from history
    # Update reached_assessment
    op.execute("""
        UPDATE jobapplication 
        SET reached_assessment = true 
        WHERE id IN (
            SELECT application_id FROM applicationevent WHERE new_status = 'ASSESSMENT'
        )
    """)
    # Update reached_interview
    op.execute("""
        UPDATE jobapplication 
        SET reached_interview = true 
        WHERE id IN (
            SELECT application_id FROM applicationevent WHERE new_status = 'INTERVIEW'
        )
    """)


def downgrade() -> None:
    op.drop_column('jobapplication', 'reached_interview')
    op.drop_column('jobapplication', 'reached_assessment')