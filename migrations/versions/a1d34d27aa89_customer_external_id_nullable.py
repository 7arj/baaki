"""customer external_id nullable

`customers.external_id` defaulted to "" under a UNIQUE(org_id, external_id) constraint. SQL treats
NULLs as distinct but empty strings as equal, so only one customer per org could go without a code.
Make the column nullable and convert existing blanks to NULL.

Revision ID: a1d34d27aa89
Revises: c62604634d53
Created: 2026-09-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "a1d34d27aa89"
down_revision: Union[str, None] = "c62604634d53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.alter_column("external_id", existing_type=sa.VARCHAR(), nullable=True)
    op.execute("UPDATE customers SET external_id = NULL WHERE external_id = ''")


def downgrade() -> None:
    # Blanks can't be restored per-row, and NOT NULL needs a value: fall back to the customer name.
    op.execute("UPDATE customers SET external_id = substr(name, 1, 40) WHERE external_id IS NULL")
    with op.batch_alter_table("customers") as batch:
        batch.alter_column("external_id", existing_type=sa.VARCHAR(), nullable=False)
