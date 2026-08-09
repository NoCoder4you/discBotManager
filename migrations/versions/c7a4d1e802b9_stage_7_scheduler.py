"""stage 7 registered scheduler

Revision ID: c7a4d1e802b9
Revises: 912abc5e00f1
"""
from alembic import op
import sqlalchemy as sa
revision="c7a4d1e802b9"; down_revision="912abc5e00f1"; branch_labels=None; depends_on=None
def upgrade():
    op.create_table("task_schedules",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id"),nullable=False),sa.Column("task_id",sa.String(64),nullable=False),sa.Column("enabled",sa.Boolean(),nullable=False),sa.Column("schedule_type",sa.String(20),nullable=False),sa.Column("structured_config",sa.JSON(),nullable=False),sa.Column("timezone",sa.String(64),nullable=False),sa.Column("next_run_at",sa.DateTime(timezone=True)),sa.Column("last_run_at",sa.DateTime(timezone=True)),sa.Column("last_status",sa.String(20)),sa.Column("reconciliation_required",sa.Boolean(),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False),sa.UniqueConstraint("bot_id","task_id"))
    op.create_index("ix_task_schedules_bot_id","task_schedules",["bot_id"]); op.create_index("ix_task_schedules_task_id","task_schedules",["task_id"])
    op.create_table("task_runs",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("public_id",sa.String(40),nullable=False,unique=True),sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id"),nullable=False),sa.Column("task_id",sa.String(64),nullable=False),sa.Column("trigger",sa.String(20),nullable=False),sa.Column("status",sa.String(20),nullable=False),sa.Column("triggered_by_id",sa.Integer(),sa.ForeignKey("users.id")),sa.Column("actor_display",sa.String(100),nullable=False),sa.Column("operation_id",sa.String(30)),sa.Column("queued_at",sa.DateTime(timezone=True),nullable=False),sa.Column("started_at",sa.DateTime(timezone=True)),sa.Column("finished_at",sa.DateTime(timezone=True)),sa.Column("duration_ms",sa.Integer()),sa.Column("summary",sa.String(500)),sa.Column("result_metadata",sa.JSON(),nullable=False))
    for name,column in (("ix_task_runs_public_id","public_id"),("ix_task_runs_bot_id","bot_id"),("ix_task_runs_task_id","task_id"),("ix_task_runs_status","status")): op.create_index(name,"task_runs",[column])
def downgrade(): op.drop_table("task_runs"); op.drop_table("task_schedules")
