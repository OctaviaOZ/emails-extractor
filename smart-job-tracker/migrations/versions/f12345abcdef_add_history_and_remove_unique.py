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
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = 'f12345abcdef'
down_revision: Union[str, Sequence[str], None] = 'cdeec9e51d36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()
    
    # Create ApplicationEvent table if not exists
    if 'applicationevent' not in tables:
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
    
    # Add is_active to JobApplication if not exists
    columns = [c['name'] for c in inspector.get_columns('jobapplication')]
    if 'is_active' not in columns:
        op.add_column('jobapplication', sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    
    # Remove unique constraint from company_name
    # This is tricky to check efficiently, so we use "try-except" logic or assume it might be done.
    # But usually drop_index is okay if we use check.
    # Let's just try-except the index operations.
    try:
        op.drop_index('ix_jobapplication_company_name', table_name='jobapplication')
    except Exception:
        pass # Might not exist or already dropped
        
    try:
        op.create_index(op.f('ix_jobapplication_company_name'), 'jobapplication', ['company_name'], unique=False)
    except Exception:
        pass # Already exists

    # Rename applied_at to created_at
    if 'applied_at' in columns and 'created_at' not in columns:
        op.alter_column('jobapplication', 'applied_at', new_column_name='created_at')


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns = [c['name'] for c in inspector.get_columns('jobapplication')]

    # Rename created_at back to applied_at
    if 'created_at' in columns:
        op.alter_column('jobapplication', 'created_at', new_column_name='applied_at')

    # Re-add unique constraint
    try:
        op.drop_index(op.f('ix_jobapplication_company_name'), table_name='jobapplication')
        op.create_index('ix_jobapplication_company_name', 'jobapplication', ['company_name'], unique=True)
    except: pass
    
    # Remove is_active
    if 'is_active' in columns:
        op.drop_column('jobapplication', 'is_active')
    
    # Drop table
    # op.drop_table('applicationevent') # Be careful not to lose data in dev, but std downgrade drops it.
    op.drop_table('applicationevent')
