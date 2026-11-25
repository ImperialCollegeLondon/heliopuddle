"""storage_location.resolved_root

Revision ID: 0002
Revises: 0001

The credential-free root a storage location last resolved to, for storage
locations configured through Prefect blocks (STO-19). Their configured
``url`` (``prefect-block:<name>`` or a placeholder) stays the same when the
block's base path changes, so this column is what detects that the data
moved (SVC-12). NULL until first resolved.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``storage_location.resolved_root``."""
    with op.batch_alter_table("storage_location", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resolved_root", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    """Drop ``storage_location.resolved_root``."""
    with op.batch_alter_table("storage_location", schema=None) as batch_op:
        batch_op.drop_column("resolved_root")
