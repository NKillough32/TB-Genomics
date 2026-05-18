"""add case clinical infectiousness fields

Adds symptom onset, treatment start, smear status, cavitation, culture status,
culture positivity duration, and infectiousness notes to the cases table.
These fields enable proper infectious period estimation and infectiousness
weighting in the transmission evidence engine.

Revision ID: 0004_case_clinical_fields
Revises: 0003_case_pair_reviews
Create Date: 2026-05-18
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0004_case_clinical_fields"
down_revision: Union[str, Sequence[str], None] = "0003_case_pair_reviews"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    statements = [
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS symptom_onset_date DATE",
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS treatment_start_date DATE",
        (
            "ALTER TABLE cases ADD COLUMN IF NOT EXISTS smear_status TEXT "
            "CHECK (smear_status IN ('positive', 'negative', 'not_done', 'unknown')) "
            "DEFAULT 'unknown'"
        ),
        (
            "ALTER TABLE cases ADD COLUMN IF NOT EXISTS cavitation_status TEXT "
            "CHECK (cavitation_status IN ('present', 'absent', 'unknown')) "
            "DEFAULT 'unknown'"
        ),
        (
            "ALTER TABLE cases ADD COLUMN IF NOT EXISTS culture_status TEXT "
            "CHECK (culture_status IN ('positive', 'negative', 'not_done', 'unknown')) "
            "DEFAULT 'unknown'"
        ),
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS culture_positivity_duration_days INTEGER",
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS infectiousness_notes TEXT",
    ]
    for statement in statements:
        op.execute(text(statement))


def downgrade() -> None:
    cols = [
        "symptom_onset_date",
        "treatment_start_date",
        "smear_status",
        "cavitation_status",
        "culture_status",
        "culture_positivity_duration_days",
        "infectiousness_notes",
    ]
    for col in cols:
        op.execute(text(f"ALTER TABLE cases DROP COLUMN IF EXISTS {col}"))
