"""stage 3a durable process instances"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "38e4ca91c3a0"
down_revision: Union[str, None] = "b2f90c1d2a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade():
    op.create_table("bot_instances",
        sa.Column("id",sa.Integer(),primary_key=True), sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id"),nullable=False),
        sa.Column("instance_id",sa.String(41),nullable=False), sa.Column("pid",sa.Integer()), sa.Column("process_created_at",sa.DateTime(timezone=True)),
        sa.Column("started_at",sa.DateTime(timezone=True),nullable=False), sa.Column("ended_at",sa.DateTime(timezone=True)), sa.Column("exit_code",sa.Integer()),
        sa.Column("expected_running",sa.Boolean(),nullable=False,server_default=sa.false()), sa.Column("state",sa.String(20),nullable=False,server_default="offline"),
        sa.Column("python_executable",sa.String(500),nullable=False), sa.Column("entry_file",sa.String(500),nullable=False),
        sa.Column("working_directory",sa.String(500),nullable=False), sa.Column("supervisor_instance_id",sa.String(41)))
    op.create_index("ix_bot_instances_bot_id","bot_instances",["bot_id"]); op.create_index("ix_bot_instances_instance_id","bot_instances",["instance_id"],unique=True)
    op.create_index("ix_bot_instances_pid","bot_instances",["pid"]); op.create_index("ix_bot_instances_expected_running","bot_instances",["expected_running"])

def downgrade(): op.drop_table("bot_instances")
