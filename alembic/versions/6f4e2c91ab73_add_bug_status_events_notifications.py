"""add bug status events and notification deliveries

Revision ID: 6f4e2c91ab73
Revises: 7b260728a1f0
Create Date: 2026-07-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f4e2c91ab73"
down_revision: Union[str, None] = "7b260728a1f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bug_issue_status_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issue_id", sa.Uuid(), nullable=False),
        sa.Column("source_system", sa.String(length=32), nullable=False),
        sa.Column("previous_status", sa.String(length=64), nullable=False),
        sa.Column("new_status", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("event_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["bug_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bug_issue_events_issue_created",
        "bug_issue_status_events",
        ["issue_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "bug_notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("status_event_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("recipient_key", sa.String(length=191), nullable=False),
        sa.Column("session_id", sa.String(length=191), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["status_event_id"], ["bug_issue_status_events.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["bug_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "status_event_id",
            name="uq_bug_notification_subscription_event",
        ),
    )
    op.create_index(
        "ix_bug_notifications_recipient_status",
        "bug_notification_deliveries",
        ["channel", "recipient_key", "status"],
        unique=False,
    )
    op.create_index(
        "ix_bug_notifications_status_created",
        "bug_notification_deliveries",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bug_notifications_status_created",
        table_name="bug_notification_deliveries",
    )
    op.drop_index(
        "ix_bug_notifications_recipient_status",
        table_name="bug_notification_deliveries",
    )
    op.drop_table("bug_notification_deliveries")
    op.drop_index(
        "ix_bug_issue_events_issue_created", table_name="bug_issue_status_events"
    )
    op.drop_table("bug_issue_status_events")
