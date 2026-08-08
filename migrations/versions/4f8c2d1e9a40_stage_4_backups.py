"""stage 4 backup and recovery

Revision ID: 4f8c2d1e9a40
Revises: 7d2f6c41a301
"""
from alembic import op
import sqlalchemy as sa

revision="4f8c2d1e9a40"; down_revision="7d2f6c41a301"; branch_labels=None; depends_on=None

backup_type=sa.Enum("MANUAL","PRE_EDIT","PRE_RESTORE","AUTOMATIC","HOURLY","DAILY","WEEKLY","MONTHLY","SYSTEM",name="backuptype")
verification=sa.Enum("UNVERIFIED","VERIFIED","FAILED",name="verificationstatus")
restore_policy=sa.Enum("REQUIRES_STOP","SUPPORTS_LIVE",name="restorepolicy")

def upgrade():
    with op.batch_alter_table("bots") as batch:
        batch.add_column(sa.Column("backup_include",sa.JSON(),nullable=False,server_default='["**/*"]'))
        batch.add_column(sa.Column("backup_exclude",sa.JSON(),nullable=False,server_default="[]"))
        batch.add_column(sa.Column("restore_policy",restore_policy,nullable=False,server_default="REQUIRES_STOP"))
        batch.add_column(sa.Column("source_version",sa.String(100),nullable=True))
    op.create_table("backups",
        sa.Column("id",sa.Integer(),primary_key=True),sa.Column("public_id",sa.String(30),nullable=False),sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id"),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("created_by_id",sa.Integer(),sa.ForeignKey("users.id")),sa.Column("backup_type",backup_type,nullable=False),
        sa.Column("reason",sa.String(200)),sa.Column("source_version",sa.String(100)),sa.Column("size_bytes",sa.Integer(),nullable=False),sa.Column("file_count",sa.Integer(),nullable=False),
        sa.Column("verification_status",verification,nullable=False),sa.Column("verification_error",sa.String(255)),sa.Column("pinned",sa.Boolean(),nullable=False),sa.Column("protected",sa.Boolean(),nullable=False),
        sa.Column("restore_count",sa.Integer(),nullable=False),sa.Column("operation_id",sa.String(30)),sa.Column("archive_name",sa.String(100),nullable=False),sa.Column("manifest_name",sa.String(100),nullable=False))
    op.create_index("ix_backups_public_id","backups",["public_id"],unique=True); op.create_index("ix_backups_bot_id","backups",["bot_id"]); op.create_index("ix_backups_created_at","backups",["created_at"]); op.create_index("ix_backups_backup_type","backups",["backup_type"]); op.create_index("ix_backups_verification_status","backups",["verification_status"]); op.create_index("ix_backups_pinned","backups",["pinned"]); op.create_index("ix_backups_protected","backups",["protected"])

def downgrade():
    op.drop_table("backups")
    with op.batch_alter_table("bots") as batch:
        batch.drop_column("source_version"); batch.drop_column("restore_policy"); batch.drop_column("backup_exclude"); batch.drop_column("backup_include")
    restore_policy.drop(op.get_bind(),checkfirst=True); verification.drop(op.get_bind(),checkfirst=True); backup_type.drop(op.get_bind(),checkfirst=True)
