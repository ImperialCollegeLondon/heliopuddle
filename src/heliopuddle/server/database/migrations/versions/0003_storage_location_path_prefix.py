"""storage_location.path_prefix

Revision ID: 0003
Revises: 0002

The project folder a storage location's root holds (STO-23); ``''`` for the
whole project. A changed prefix moves every copy, like a changed ``url``
(SVC-12), so it is stored to be compared at config sync.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``storage_location.path_prefix`` (``''`` for existing rows)."""
    with op.batch_alter_table("storage_location", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("path_prefix", sa.String(length=1024), nullable=False, server_default="")
        )


def downgrade() -> None:
    """Drop ``storage_location.path_prefix``."""
    with op.batch_alter_table("storage_location", schema=None) as batch_op:
        batch_op.drop_column("path_prefix")
