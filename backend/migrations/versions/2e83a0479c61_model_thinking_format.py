"""Add explicit model thinking request compatibility.

Revision ID: 2e83a0479c61
Revises: 1a5c828a6b97
"""
from alembic import op
import sqlalchemy as sa

revision = "2e83a0479c61"
down_revision = "1a5c828a6b97"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_connections", sa.Column("thinking_format", sa.String(length=40), server_default="default", nullable=False))


def downgrade():
    op.drop_column("model_connections", "thinking_format")
