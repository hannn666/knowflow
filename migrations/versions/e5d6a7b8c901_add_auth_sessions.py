"""Add revocable authentication sessions and single-use refresh tokens.

Revision ID: e5d6a7b8c901
Revises: c842a9f713e0
"""
from alembic import op
import sqlalchemy as sa

revision = "e5d6a7b8c901"
down_revision = "c842a9f713e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revocation_reason", sa.String(16)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("absolute_expires_at > created_at", name="ck_auth_session_absolute"),
        sa.CheckConstraint("idle_expires_at > created_at AND idle_expires_at <= absolute_expires_at",
                           name="ck_auth_session_idle"),
        sa.CheckConstraint("(revoked_at IS NULL AND revocation_reason IS NULL) OR "
                           "(revoked_at IS NOT NULL AND revocation_reason IS NOT NULL "
                           "AND revocation_reason IN ('logout', 'replay'))",
                           name="ck_auth_session_revocation"))
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table("auth_refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("parent_token_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["session_id"], ["auth_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_token_id"], ["auth_refresh_tokens.id"]),
        sa.UniqueConstraint("token_hash"), sa.UniqueConstraint("parent_token_id"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_auth_refresh_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_refresh_expiry"),
        sa.CheckConstraint("consumed_at IS NULL OR consumed_at >= created_at", name="ck_auth_refresh_consumed"))
    op.create_index("ix_auth_refresh_tokens_session_id", "auth_refresh_tokens", ["session_id"])
    op.create_index("uq_auth_refresh_active", "auth_refresh_tokens", ["session_id"], unique=True,
                    postgresql_where=sa.text("consumed_at IS NULL"), sqlite_where=sa.text("consumed_at IS NULL"))


def downgrade():
    op.drop_table("auth_refresh_tokens")
    op.drop_table("auth_sessions")
