"""covering index for per-kind metrics

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_spans_project_kind_start",
        "spans",
        ["project_id", "kind", "start_time"],
        postgresql_include=["duration_ms", "status", "error_type", "retry_count"],
    )
    op.drop_index("ix_spans_project_kind", table_name="spans")


def downgrade() -> None:
    op.create_index("ix_spans_project_kind", "spans", ["project_id", "kind"])
    op.drop_index("ix_spans_project_kind_start", table_name="spans")
