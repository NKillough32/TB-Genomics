"""add operational indexes and HSC Trust lookup

Revision ID: 0009_operational_indexes_trusts
Revises: 0008_case_data_mgmt
Create Date: 2026-06-15
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0009_operational_indexes_trusts"
down_revision: Union[str, Sequence[str], None] = "0008_case_data_mgmt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TRUST_ROWS = [
    ("BHSCT", "Belfast Health and Social Care Trust"),
    ("NHSCT", "Northern Health and Social Care Trust"),
    ("SEHSCT", "South Eastern Health and Social Care Trust"),
    ("SHSCT", "Southern Health and Social Care Trust"),
    ("WHSCT", "Western Health and Social Care Trust"),
]


def upgrade() -> None:
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_cases_specimen_date ON cases (specimen_date)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_cases_geographic_region ON cases (geographic_region)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_cases_region_specimen_date ON cases (geographic_region, specimen_date)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_tb_interpretation_lineage ON tb_interpretation (lineage)"))
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS hsc_trusts (
          trust_code TEXT PRIMARY KEY,
          trust_name TEXT NOT NULL,
          active BOOLEAN NOT NULL DEFAULT TRUE
        )
    """))
    for code, name in TRUST_ROWS:
        op.execute(
            text("""
                INSERT INTO hsc_trusts (trust_code, trust_name, active)
                VALUES (:code, :name, TRUE)
                ON CONFLICT (trust_code)
                DO UPDATE SET trust_name = EXCLUDED.trust_name, active = TRUE
            """),
            {"code": code, "name": name},
        )


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS hsc_trusts"))
    op.execute(text("DROP INDEX IF EXISTS ix_tb_interpretation_lineage"))
    op.execute(text("DROP INDEX IF EXISTS ix_cases_region_specimen_date"))
    op.execute(text("DROP INDEX IF EXISTS ix_cases_geographic_region"))
    op.execute(text("DROP INDEX IF EXISTS ix_cases_specimen_date"))
