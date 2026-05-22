"""add normalized resistance calls, alerts, and actions

Revision ID: 0007_resistance_alerts_actions
Revises: 0006_add_performance_indexes
Create Date: 2026-05-22
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "0007_resistance_alerts_actions"
down_revision: Union[str, Sequence[str], None] = "0006_add_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS resistance_calls (
              call_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
              sample_id UUID NOT NULL REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
              drug TEXT NOT NULL,
              gene TEXT,
              mutation TEXT,
              prediction TEXT,
              confidence NUMERIC,
              depth NUMERIC,
              alt_fraction NUMERIC,
              lineage TEXT,
              source_tool TEXT NOT NULL DEFAULT 'tbprofiler',
              tool_version TEXT,
              database_version TEXT,
              source_path TEXT,
              raw_call JSONB NOT NULL DEFAULT '{}',
              created_at TIMESTAMP DEFAULT NOW(),
              UNIQUE (sample_id, drug, gene, mutation, source_tool)
            )
            """
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_resistance_calls_sample ON resistance_calls (sample_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_resistance_calls_drug_prediction ON resistance_calls (drug, prediction)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_resistance_calls_source ON resistance_calls (source_tool)"))

    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS alerts (
              alert_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
              alert_type TEXT NOT NULL,
              severity TEXT NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
              status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
              sample_id UUID REFERENCES cases(pseudonymised_case_id) ON DELETE CASCADE,
              cluster_id UUID REFERENCES clusters(cluster_id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              description TEXT,
              evidence JSONB NOT NULL DEFAULT '{}',
              assigned_to TEXT,
              acknowledged_by TEXT,
              acknowledged_at TIMESTAMP,
              resolved_by TEXT,
              resolved_at TIMESTAMP,
              created_at TIMESTAMP DEFAULT NOW(),
              updated_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts (status)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_type ON alerts (alert_type)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_cluster ON alerts (cluster_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_sample ON alerts (sample_id)"))

    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS actions (
              action_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
              alert_id UUID REFERENCES alerts(alert_id) ON DELETE SET NULL,
              cluster_id UUID REFERENCES clusters(cluster_id) ON DELETE SET NULL,
              sample_id UUID REFERENCES cases(pseudonymised_case_id) ON DELETE SET NULL,
              action_type TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'completed', 'cancelled')),
              owner TEXT,
              note TEXT,
              due_at TIMESTAMP,
              completed_at TIMESTAMP,
              created_by TEXT,
              created_at TIMESTAMP DEFAULT NOW(),
              updated_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
    )
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_actions_status ON actions (status)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_actions_alert ON actions (alert_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_actions_cluster ON actions (cluster_id)"))
    op.execute(text("CREATE INDEX IF NOT EXISTS ix_actions_sample ON actions (sample_id)"))


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS actions"))
    op.execute(text("DROP TABLE IF EXISTS alerts"))
    op.execute(text("DROP TABLE IF EXISTS resistance_calls"))
