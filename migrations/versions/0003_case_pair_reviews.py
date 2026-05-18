"""add case pair reviewer classifications

Revision ID: 0003_case_pair_reviews
Revises: 0002_epidemiology_tables
Create Date: 2026-05-18
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0003_case_pair_reviews"
down_revision: Union[str, Sequence[str], None] = "0002_epidemiology_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS case_pair_reviews (
          case_a UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
          case_b UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
          reviewer_classification TEXT NOT NULL CHECK (
            reviewer_classification IN (
              'confirmed transmission',
              'probable transmission',
              'possible transmission',
              'unlikely transmission',
              'insufficient evidence'
            )
          ),
          reviewer TEXT NOT NULL,
          notes TEXT,
          source_cluster_id UUID REFERENCES clusters(cluster_id) ON DELETE SET NULL,
          reviewed_at TIMESTAMP DEFAULT NOW(),
          PRIMARY KEY (case_a, case_b),
          CHECK (case_a <> case_b)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_case_pair_reviews_cluster ON case_pair_reviews (source_cluster_id)",
        "CREATE INDEX IF NOT EXISTS ix_case_pair_reviews_reviewed_at ON case_pair_reviews (reviewed_at DESC)",
    ]
    for statement in statements:
        op.execute(text(statement))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS case_pair_reviews CASCADE")
