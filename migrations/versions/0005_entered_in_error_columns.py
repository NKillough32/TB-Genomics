"""add entered-in-error soft-delete columns

Revision ID: 0005_entered_in_error_columns
Revises: 0004_case_clinical_fields
Create Date: 2026-05-18
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0005_entered_in_error_columns"
down_revision: Union[str, Sequence[str], None] = "0004_case_clinical_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TARGET_TABLES = (
    "contacts",
    "locations",
    "exposures",
    "case_location_events",
    "case_contact_links",
    "case_pair_reviews",
)


def upgrade() -> None:
    for table in _TARGET_TABLES:
        op.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS entered_in_error BOOLEAN NOT NULL DEFAULT FALSE"))
        op.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS entered_in_error_at TIMESTAMP"))
        op.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS entered_in_error_by TEXT"))
        op.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS entered_in_error_reason TEXT"))


def downgrade() -> None:
    for table in _TARGET_TABLES:
        op.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS entered_in_error_reason"))
        op.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS entered_in_error_by"))
        op.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS entered_in_error_at"))
        op.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS entered_in_error"))
