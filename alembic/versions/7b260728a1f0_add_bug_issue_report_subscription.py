"""add bug issue report subscription

Revision ID: 7b260728a1f0
Revises: 18d131f67f3e
Create Date: 2026-07-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b260728a1f0"
down_revision: Union[str, None] = "18d131f67f3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bug_issues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("external_record_id", sa.String(length=191), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("module", sa.String(length=255), nullable=False),
        sa.Column("normalized_description", sa.Text(), nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("issue_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_snapshot", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_system", "external_record_id", name="uq_bug_issue_external"
        ),
    )
    op.create_index(
        "ix_bug_issues_status_updated",
        "bug_issues",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "bug_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("issue_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("link_type", sa.String(length=32), nullable=False),
        sa.Column("external_record_id", sa.String(length=191), nullable=False),
        sa.Column("report_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["draft_id"], ["bug_drafts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["bug_issues.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", name="uq_bug_report_draft"),
    )
    op.create_index(
        "ix_bug_reports_issue_created",
        "bug_reports",
        ["issue_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_bug_reports_status_created",
        "bug_reports",
        ["status", "created_at"],
        unique=False,
    )
    op.create_table(
        "bug_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issue_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("subscriber_key", sa.String(length=191), nullable=False),
        sa.Column("user_key", sa.String(length=191), nullable=False),
        sa.Column("session_id", sa.String(length=191), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["bug_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_id", "channel", "subscriber_key", name="uq_bug_subscription"
        ),
    )
    op.create_index(
        "ix_bug_subscriptions_status_updated",
        "bug_subscriptions",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_bug_subscriptions_status_updated", table_name="bug_subscriptions")
    op.drop_table("bug_subscriptions")
    op.drop_index("ix_bug_reports_status_created", table_name="bug_reports")
    op.drop_index("ix_bug_reports_issue_created", table_name="bug_reports")
    op.drop_table("bug_reports")
    op.drop_index("ix_bug_issues_status_updated", table_name="bug_issues")
    op.drop_table("bug_issues")
