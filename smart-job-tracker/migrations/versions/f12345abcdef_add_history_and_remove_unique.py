"""Add history table and remove unique constraint on company

Revision ID: f12345abcdef
Revises: cdeec9e51d36
Create Date: 2026-01-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f12345abcdef'
down_revision: Union[str, Sequence[str], None] = 'cdeec9e51d36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create ApplicationEvent table
    op.create_table('applicationevent',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('event_date', sa.DateTime(), nullable=False),
        sa.Column('old_status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('new_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('email_subject', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['jobapplication.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Add is_active to JobApplication
    op.add_column('jobapplication', sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    
    # Remove unique constraint from company_name
    op.drop_index('ix_jobapplication_company_name', table_name='jobapplication')
    op.create_index(op.f('ix_jobapplication_company_name'), 'jobapplication', ['company_name'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Re-add unique constraint
    op.drop_index(op.f('ix_jobapplication_company_name'), table_name='jobapplication')
    op.create_index('ix_jobapplication_company_name', 'jobapplication', ['company_name'], unique=True)
    
    # Remove is_active
    op.drop_column('jobapplication', 'is_active')
    
    # Drop table
    op.drop_table('applicationevent')
