"""baseline existing PoC schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-16
"""

from typing import Sequence, Union


revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Baseline migration for deployments that already use db/schema.sql or
    # SQLAlchemy create_all. Future schema changes should be expressed as
    # incremental Alembic revisions instead of editing live tables ad hoc.
    pass


def downgrade() -> None:
    pass
