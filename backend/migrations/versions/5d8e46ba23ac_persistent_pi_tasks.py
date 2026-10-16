"""Persistent Pi logical tasks and append-only checkpoints.

Revision ID: 5d8e46ba23ac
Revises: 4c7b35a912df
"""
from alembic import op
import sqlalchemy as sa

revision = "5d8e46ba23ac"
down_revision = "4c7b35a912df"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_tasks",
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("granted_seconds", sa.Float(), nullable=False),
        sa.Column("granted_tool_calls", sa.Integer(), nullable=False),
        sa.Column("elapsed_seconds", sa.Float(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("allocations", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("clock_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clock_lease_token", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_task_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("agent_tasks.job_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(200), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "sequence"),
        sa.UniqueConstraint("job_id", "request_id"),
    )
    op.create_index("ix_agent_task_events_job_id", "agent_task_events", ["job_id"])


def downgrade():
    op.drop_table("agent_task_events")
    op.drop_table("agent_tasks")
