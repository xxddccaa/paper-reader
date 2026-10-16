"""Add guided workflows, durable stage records and structured reading packets.

Revision ID: 3f9b52d018e7
Revises: 2e83a0479c61
"""
from alembic import op
import sqlalchemy as sa

revision = "3f9b52d018e7"
down_revision = "2e83a0479c61"
branch_labels = None
depends_on = None


def upgrade():
    # Existing results were produced by the native skill workflow.
    op.add_column("readings", sa.Column("workflow_mode", sa.String(length=16), server_default="native", nullable=False))
    op.alter_column("readings", "workflow_mode", server_default="guided")
    op.add_column("jobs", sa.Column("stage_records", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("revisions", sa.Column("reading_packet", sa.JSON(), server_default="{}", nullable=False))


def downgrade():
    op.drop_column("revisions", "reading_packet")
    op.drop_column("jobs", "stage_records")
    op.drop_column("readings", "workflow_mode")
