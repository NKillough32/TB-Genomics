"""baseline existing PoC schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-16
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    schema_sql = """
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

    CREATE TABLE IF NOT EXISTS cases (
      pseudonymised_case_id UUID PRIMARY KEY,
      local_lab_sample_id TEXT UNIQUE,
      specimen_date DATE,
      geographic_region TEXT,
      case_status TEXT,
      created_at TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS tb_interpretation (
      sample_id UUID PRIMARY KEY REFERENCES cases(pseudonymised_case_id),
      species_confirmation TEXT,
      lineage TEXT,
      sublineage TEXT,
      resistance_mutations JSONB,
      predicted_drug_resistance JSONB,
      confidence_score NUMERIC,
      interpretation_summary TEXT
    );

    CREATE TABLE IF NOT EXISTS consensus_sequences (
      sample_id UUID PRIMARY KEY REFERENCES cases(pseudonymised_case_id),
      sequence TEXT,
      length INTEGER
    );

    CREATE TABLE IF NOT EXISTS sequencing_runs (
      run_id TEXT PRIMARY KEY,
      platform TEXT,
      instrument_name TEXT,
      pipeline_version TEXT,
      reference_genome TEXT,
      started_at TIMESTAMP,
      completed_at TIMESTAMP,
      created_at TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS sample_qc_metrics (
      sample_id UUID PRIMARY KEY REFERENCES cases(pseudonymised_case_id),
      run_id TEXT REFERENCES sequencing_runs(run_id),
      mean_depth NUMERIC,
      coverage_breadth NUMERIC,
      ambiguous_base_percent NUMERIC,
      contamination_flag BOOLEAN DEFAULT FALSE,
      qc_status TEXT,
      qc_failure_reason TEXT,
      reported_at TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS analysis_provenance (
      provenance_id SERIAL PRIMARY KEY,
      sample_id UUID REFERENCES cases(pseudonymised_case_id),
      pipeline_name TEXT,
      pipeline_version TEXT,
      reference_genome TEXT,
      software_versions JSONB,
      parameters JSONB,
      generated_at TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS clusters (
      cluster_id UUID PRIMARY KEY,
      snp_distance INTEGER,
      investigation_status TEXT,
      alert_flag BOOLEAN
    );

    CREATE TABLE IF NOT EXISTS case_clusters (
      sample_id UUID REFERENCES cases(pseudonymised_case_id),
      cluster_id UUID REFERENCES clusters(cluster_id),
      PRIMARY KEY (sample_id, cluster_id)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
      audit_id SERIAL PRIMARY KEY,
      action TEXT,
      user_id TEXT,
      details JSONB,
      timestamp TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS pipeline_validation_signoffs (
      id SERIAL PRIMARY KEY,
      pipeline TEXT NOT NULL DEFAULT 'resistance_validation',
      decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'under_review')),
      reviewer TEXT NOT NULL,
      notes TEXT,
      catalogue_version TEXT,
      signed_off_at TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS cluster_investigations (
      investigation_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
      cluster_id UUID NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
      risk_score NUMERIC NOT NULL DEFAULT 0,
      risk_band TEXT NOT NULL DEFAULT 'low' CHECK (risk_band IN ('low', 'medium', 'high', 'critical')),
      assigned_to TEXT,
      status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'under_review', 'signed_off', 'closed')),
      epi_notes TEXT,
      actions JSONB NOT NULL DEFAULT '[]',
      decision TEXT,
      decision_by TEXT,
      decision_at TIMESTAMP,
      created_at TIMESTAMP DEFAULT NOW(),
      updated_at TIMESTAMP DEFAULT NOW()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_cluster_investigations_cluster
      ON cluster_investigations (cluster_id);
    """
    for statement in schema_sql.split(";"):
        if statement.strip():
            op.execute(text(statement))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cluster_investigations CASCADE")
    op.execute("DROP TABLE IF EXISTS pipeline_validation_signoffs CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS case_clusters CASCADE")
    op.execute("DROP TABLE IF EXISTS clusters CASCADE")
    op.execute("DROP TABLE IF EXISTS analysis_provenance CASCADE")
    op.execute("DROP TABLE IF EXISTS sample_qc_metrics CASCADE")
    op.execute("DROP TABLE IF EXISTS sequencing_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS consensus_sequences CASCADE")
    op.execute("DROP TABLE IF EXISTS tb_interpretation CASCADE")
    op.execute("DROP TABLE IF EXISTS cases CASCADE")
