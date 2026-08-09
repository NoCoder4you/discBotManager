"""stage 9 discord guild snapshots

Revision ID: e9f6a8b02345
Revises: d8e5f7a91234
"""
from alembic import op
import sqlalchemy as sa
revision='e9f6a8b02345'; down_revision='d8e5f7a91234'; branch_labels=None; depends_on=None
def upgrade():
    op.create_table('discord_guild_snapshots',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('bot_id',sa.String(36),sa.ForeignKey('bots.id'),nullable=False),sa.Column('guild_id',sa.String(32),nullable=False),sa.Column('instance_id',sa.String(41),nullable=False),sa.Column('generated_at',sa.DateTime(timezone=True),nullable=False),sa.Column('received_at',sa.DateTime(timezone=True),nullable=False),sa.Column('payload',sa.JSON(),nullable=False),sa.Column('diagnostics',sa.JSON(),nullable=False),sa.UniqueConstraint('bot_id','guild_id',name='uq_discord_snapshot_bot_guild'))
    op.create_index('ix_discord_guild_snapshots_bot_id','discord_guild_snapshots',['bot_id']); op.create_index('ix_discord_guild_snapshots_guild_id','discord_guild_snapshots',['guild_id']); op.create_index('ix_discord_guild_snapshots_instance_id','discord_guild_snapshots',['instance_id'])
    op.create_table('discord_diagnostic_states',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('bot_id',sa.String(36),sa.ForeignKey('bots.id'),nullable=False),sa.Column('guild_id',sa.String(32),nullable=False),sa.Column('fingerprint',sa.String(64),nullable=False),sa.Column('status',sa.String(12),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),sa.UniqueConstraint('bot_id','fingerprint',name='uq_discord_diagnostic_state'))
    op.create_index('ix_discord_diagnostic_states_bot_id','discord_diagnostic_states',['bot_id']); op.create_index('ix_discord_diagnostic_states_guild_id','discord_diagnostic_states',['guild_id'])
def downgrade():
    op.drop_table('discord_diagnostic_states'); op.drop_table('discord_guild_snapshots')
