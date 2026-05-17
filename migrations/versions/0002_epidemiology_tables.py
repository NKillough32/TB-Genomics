"""add structured epidemiology tables

Revision ID: 0002_epidemiology_tables
Revises: 0001_baseline
Create Date: 2026-05-17
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0002_epidemiology_tables"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS case_contacts (
          contact_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          source_case_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
          contact_case_id UUID REFERENCES cases(pseudonymised_case_id) ON DELETE SET NULL,
          contact_label TEXT,
          relationship_type TEXT,
          exposure_start_date DATE,
          exposure_end_date DATE,
          setting TEXT,
          confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
          notes TEXT,
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS case_exposures (
          exposure_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          case_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
          exposure_type TEXT NOT NULL,
          location_name TEXT,
          location_address TEXT,
          geographic_region TEXT,
          exposure_start_date DATE,
          exposure_end_date DATE,
          confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
          source TEXT,
          notes TEXT,
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS location_events (
          event_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          case_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
          location_name TEXT NOT NULL,
          location_type TEXT,
          location_address TEXT,
          geographic_region TEXT,
          arrived_at TIMESTAMP,
          departed_at TIMESTAMP,
          source TEXT,
          confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_case_contacts_source_case ON case_contacts (source_case_id)",
        "CREATE INDEX IF NOT EXISTS ix_case_contacts_contact_case ON case_contacts (contact_case_id)",
        "CREATE INDEX IF NOT EXISTS ix_case_exposures_case ON case_exposures (case_id)",
        "CREATE INDEX IF NOT EXISTS ix_case_exposures_region ON case_exposures (geographic_region)",
        "CREATE INDEX IF NOT EXISTS ix_location_events_case ON location_events (case_id)",
        "CREATE INDEX IF NOT EXISTS ix_location_events_region ON location_events (geographic_region)",
    ]
    for statement in statements:
        op.execute(text(statement))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS location_events CASCADE")
    op.execute("DROP TABLE IF EXISTS case_exposures CASCADE")
    op.execute("DROP TABLE IF EXISTS case_contacts CASCADE")
