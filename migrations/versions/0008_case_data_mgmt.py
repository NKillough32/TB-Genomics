"""add case data management controls

Revision ID: 0008_case_data_mgmt
Revises: 0007_resistance_alerts_actions
Create Date: 2026-05-27
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0008_case_data_mgmt"
down_revision: Union[str, Sequence[str], None] = "0007_resistance_alerts_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS entered_in_error BOOLEAN NOT NULL DEFAULT FALSE"))
    op.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS entered_in_error_at TIMESTAMP"))
    op.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS entered_in_error_by TEXT"))
    op.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS entered_in_error_reason TEXT"))
    op.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_cases_entered_in_error ON cases (entered_in_error)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_cases_updated_at ON cases (updated_at DESC)"))


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_cases_updated_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_cases_entered_in_error"))
    op.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS updated_at"))
    op.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS entered_in_error_reason"))
    op.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS entered_in_error_by"))
    op.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS entered_in_error_at"))
    op.execute(text("ALTER TABLE cases DROP COLUMN IF EXISTS entered_in_error"))
