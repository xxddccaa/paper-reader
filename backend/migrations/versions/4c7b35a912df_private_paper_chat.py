"""Private paper conversations and durable chat messages.

Revision ID: 4c7b35a912df
Revises: 3f9b52d018e7
"""
from alembic import op
import sqlalchemy as sa

revision = "4c7b35a912df"
down_revision = "3f9b52d018e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reading_id", sa.String(36), sa.ForeignKey("readings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for field in ("workspace_id", "reading_id", "user_id"):
        op.create_index(f"ix_chat_conversations_{field}", "chat_conversations", [field])
    op.add_column("jobs", sa.Column("conversation_id", sa.String(36), nullable=True))
    op.create_foreign_key("fk_jobs_conversation_id", "jobs", "chat_conversations", ["conversation_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_jobs_conversation_id", "jobs", ["conversation_id"])
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("evidence_index", sa.JSON(), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(36), nullable=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "sequence"),
        sa.UniqueConstraint("conversation_id", "request_id"),
    )
    for field in ("conversation_id", "workspace_id", "status", "job_id"):
        op.create_index(f"ix_chat_messages_{field}", "chat_messages", [field])


def downgrade():
    op.drop_table("chat_messages")
    op.drop_index("ix_jobs_conversation_id", table_name="jobs")
    op.drop_constraint("fk_jobs_conversation_id", "jobs", type_="foreignkey")
    op.drop_column("jobs", "conversation_id")
    op.drop_table("chat_conversations")
