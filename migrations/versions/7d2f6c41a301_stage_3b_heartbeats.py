"""stage 3b Discord heartbeats

Revision ID: 7d2f6c41a301
Revises: 38e4ca91c3a0
"""
from alembic import op
import sqlalchemy as sa
revision='7d2f6c41a301'; down_revision='38e4ca91c3a0'; branch_labels=None; depends_on=None

def upgrade():
    op.add_column('bots',sa.Column('management_secret_hash',sa.String(64),nullable=True))
    for column in [
        sa.Column('discord_connected',sa.Boolean(),nullable=False,server_default=sa.false()),sa.Column('discord_ready',sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column('last_heartbeat_at',sa.DateTime(timezone=True)),sa.Column('last_agent_timestamp',sa.DateTime(timezone=True)),sa.Column('connected_at',sa.DateTime(timezone=True)),sa.Column('ready_at',sa.DateTime(timezone=True)),sa.Column('last_ready_at',sa.DateTime(timezone=True)),sa.Column('last_disconnect_at',sa.DateTime(timezone=True)),sa.Column('discord_latency_ms',sa.Float()),sa.Column('guild_count',sa.Integer())]: op.add_column('bot_instances',column)

def downgrade():
    for name in ['guild_count','discord_latency_ms','last_disconnect_at','last_ready_at','ready_at','connected_at','last_agent_timestamp','last_heartbeat_at','discord_ready','discord_connected']: op.drop_column('bot_instances',name)
    op.drop_column('bots','management_secret_hash')
