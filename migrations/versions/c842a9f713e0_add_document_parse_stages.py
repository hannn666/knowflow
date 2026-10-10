"""Add independent version parse-stage records.

Revision ID: c842a9f713e0
Revises: b71d4a09e6c2
"""
from alembic import op
import sqlalchemy as sa

revision = "c842a9f713e0"
down_revision = "b71d4a09e6c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_parse_stages",
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("has_text", sa.Boolean(), nullable=True),
        sa.Column("result_sha256", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("document_version_id"),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="ck_parse_stage_status"),
        sa.CheckConstraint(
            "(status = 'running' AND page_count IS NULL AND has_text IS NULL "
            "AND result_sha256 IS NULL AND error_code IS NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND page_count IS NOT NULL AND page_count > 0 AND has_text IS NOT NULL "
            "AND result_sha256 IS NOT NULL AND length(result_sha256) = 64 "
            "AND error_code IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND page_count IS NULL AND has_text IS NULL "
            "AND result_sha256 IS NULL AND error_code IS NOT NULL AND finished_at IS NOT NULL)",
            name="ck_parse_stage_result",
        ),
    )


def downgrade() -> None:
    op.drop_table("document_parse_stages")
