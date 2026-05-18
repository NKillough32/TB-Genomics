"""add performance indexes for hot query paths

Revision ID: 0006_add_performance_indexes
Revises: 0005_entered_in_error_columns
Create Date: 2026-05-20
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0006_add_performance_indexes"
down_revision: Union[str, Sequence[str], None] = "0005_entered_in_error_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create indexes on hot query paths for synthesis and reporting."""
    # Index on case_clusters.cluster_id - used in every synthesis query
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_case_clusters_cluster ON case_clusters (cluster_id)"))
    
    # Indexes on audit_log for data_safety gates and activity tracking
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_log_action ON audit_log (action)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_log_timestamp ON audit_log (timestamp DESC)"))
    
    # Index on analysis_provenance.sample_id for sample lineage tracking
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_analysis_provenance_sample ON analysis_provenance (sample_id)"))


def downgrade() -> None:
    """Drop performance indexes."""
    op.execute(text("DROP INDEX IF EXISTS ix_case_clusters_cluster"))
    op.execute(text("DROP INDEX IF EXISTS ix_audit_log_action"))
    op.execute(text("DROP INDEX IF EXISTS ix_audit_log_timestamp"))
    op.execute(text("DROP INDEX IF EXISTS ix_analysis_provenance_sample"))
