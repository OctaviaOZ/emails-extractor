"""Add PENDING to ApplicationStatus

Revision ID: 49ad2d9860ea
Revises: 9b394b58e344
Create Date: 2026-02-02 15:08:17.282442

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '49ad2d9860ea'
down_revision: Union[str, Sequence[str], None] = '9b394b58e344'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Manually add the value to the Postgres Enum type
    # We use a transaction commit before this because ALTER TYPE cannot run in a transaction block
    op.execute("COMMIT")
    op.execute("ALTER TYPE applicationstatus ADD VALUE IF NOT EXISTS 'PENDING'")


def downgrade() -> None:
    """Downgrade schema."""
    # Removing a value from an Enum in Postgres is not directly supported without dropping/recreating.
    pass
