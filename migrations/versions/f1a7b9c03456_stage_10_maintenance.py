"""stage 10 maintenance

Revision ID: f1a7b9c03456
Revises: e9f6a8b02345
"""
from alembic import op
import sqlalchemy as sa
revision="f1a7b9c03456"; down_revision="e9f6a8b02345"; branch_labels=None; depends_on=None
def upgrade():
    op.create_table("bot_maintenance",
        sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id"),primary_key=True),sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column("reason",sa.String(500)),sa.Column("public_message",sa.String(1000)),sa.Column("enabled_at",sa.DateTime(timezone=True)),sa.Column("enabled_by_id",sa.Integer(),sa.ForeignKey("users.id")),sa.Column("planned_end_at",sa.DateTime(timezone=True)),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False),
        sa.Column("applied_enabled",sa.Boolean()),sa.Column("applied_instance_id",sa.String(41)),sa.Column("applied_at",sa.DateTime(timezone=True)),sa.Column("sync_error",sa.String(255)),sa.Column("bypass_user_ids",sa.JSON(),nullable=False),sa.Column("bypass_roles",sa.JSON(),nullable=False))
    op.create_index("ix_bot_maintenance_enabled","bot_maintenance",["enabled"])
def downgrade(): op.drop_table("bot_maintenance")
