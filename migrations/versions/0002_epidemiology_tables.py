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
        CREATE TABLE IF NOT EXISTS contacts (
          contact_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          contact_label TEXT NOT NULL,
          contact_type TEXT,
          relationship_type TEXT,
          pseudonymised_identifier TEXT,
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS locations (
          location_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          location_name TEXT NOT NULL,
          location_type TEXT,
          address_line TEXT,
          geographic_region TEXT,
          postcode_prefix TEXT,
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS exposures (
          exposure_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          exposure_type TEXT NOT NULL,
          exposure_context TEXT,
          exposure_start_date DATE,
          exposure_end_date DATE,
          confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
          source TEXT,
          notes TEXT,
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS case_location_events (
          event_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          case_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
          location_id UUID NOT NULL REFERENCES locations(location_id) ON DELETE CASCADE,
          exposure_id UUID REFERENCES exposures(exposure_id) ON DELETE SET NULL,
          event_type TEXT NOT NULL,
          arrived_at TIMESTAMP,
          departed_at TIMESTAMP,
          confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
          source TEXT,
          notes TEXT,
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS case_contact_links (
          link_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
          case_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
          contact_id UUID NOT NULL REFERENCES contacts(contact_id) ON DELETE CASCADE,
          exposure_id UUID REFERENCES exposures(exposure_id) ON DELETE SET NULL,
          link_type TEXT,
          exposure_start_date DATE,
          exposure_end_date DATE,
          source TEXT,
          confidence TEXT CHECK (confidence IS NULL OR confidence IN ('low', 'medium', 'high')),
          notes TEXT,
          created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_locations_region ON locations (geographic_region)",
        "CREATE INDEX IF NOT EXISTS ix_exposures_type ON exposures (exposure_type)",
        "CREATE INDEX IF NOT EXISTS ix_case_location_events_case ON case_location_events (case_id)",
        "CREATE INDEX IF NOT EXISTS ix_case_location_events_location ON case_location_events (location_id)",
        "CREATE INDEX IF NOT EXISTS ix_case_contact_links_case ON case_contact_links (case_id)",
        "CREATE INDEX IF NOT EXISTS ix_case_contact_links_contact ON case_contact_links (contact_id)",
    ]
    for statement in statements:
        op.execute(text(statement))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS case_contact_links CASCADE")
    op.execute("DROP TABLE IF EXISTS case_location_events CASCADE")
    op.execute("DROP TABLE IF EXISTS exposures CASCADE")
    op.execute("DROP TABLE IF EXISTS locations CASCADE")
    op.execute("DROP TABLE IF EXISTS contacts CASCADE")
