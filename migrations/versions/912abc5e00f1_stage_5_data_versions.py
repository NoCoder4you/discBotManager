"""stage 5 safe data management

Revision ID: 912abc5e00f1
Revises: 4f8c2d1e9a40
"""
from alembic import op
import sqlalchemy as sa
revision="912abc5e00f1"; down_revision="4f8c2d1e9a40"; branch_labels=None; depends_on=None
def upgrade():
    op.create_table("data_versions",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id"),nullable=False),sa.Column("data_source",sa.String(100),nullable=False),sa.Column("relative_path",sa.String(500),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("actor_id",sa.Integer(),sa.ForeignKey("users.id")),sa.Column("operation_id",sa.String(30),nullable=False),sa.Column("backup_id",sa.Integer(),sa.ForeignKey("backups.id"),nullable=False),sa.Column("previous_hash",sa.String(64),nullable=False),sa.Column("new_hash",sa.String(64),nullable=False))
    for name,cols in (("ix_data_versions_bot_id",["bot_id"]),("ix_data_versions_data_source",["data_source"]),("ix_data_versions_created_at",["created_at"]),("ix_data_versions_operation_id",["operation_id"])): op.create_index(name,"data_versions",cols)
def downgrade(): op.drop_table("data_versions")
