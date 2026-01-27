"""Add COMMUNICATION to applicationstatus enum

Revision ID: 4ad32d28cac1
Revises: 8041dd8a065b
Create Date: 2026-01-27 14:00:10.021522

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '4ad32d28cac1'
down_revision: Union[str, Sequence[str], None] = '8041dd8a065b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE applicationstatus ADD VALUE 'COMMUNICATION'")


def downgrade() -> None:
    """Downgrade schema."""
    # Enums are hard to downgrade in Postgres, typically we leave them
    pass
