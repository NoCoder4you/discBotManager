"""stage 2 provisioning and operation metadata"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "b2f90c1d2a11"
down_revision: Union[str,None] = "8ce6b8bc8658"
branch_labels: Union[str,Sequence[str],None] = None
depends_on: Union[str,Sequence[str],None] = None

def upgrade():
    with op.batch_alter_table("bot_assignments") as batch:
        batch.add_column(sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()))
        batch.add_column(sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.current_timestamp()))
        batch.add_column(sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.current_timestamp()))
    with op.batch_alter_table("operations") as batch:
        batch.add_column(sa.Column("completed_at",sa.DateTime(timezone=True),nullable=True))
        batch.add_column(sa.Column("event_metadata",sa.JSON(),nullable=False,server_default="{}"))
        batch.add_column(sa.Column("error",sa.String(255),nullable=True))

def downgrade():
    with op.batch_alter_table("operations") as batch:
        batch.drop_column("error"); batch.drop_column("event_metadata"); batch.drop_column("completed_at")
    with op.batch_alter_table("bot_assignments") as batch:
        batch.drop_column("updated_at"); batch.drop_column("created_at"); batch.drop_column("enabled")
