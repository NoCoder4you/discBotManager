"""stage 8 operational incidents

Revision ID: d8e5f7a91234
Revises: c7a4d1e802b9
"""
from alembic import op
import sqlalchemy as sa
revision="d8e5f7a91234"; down_revision="c7a4d1e802b9"; branch_labels=None; depends_on=None
def upgrade():
    severity=sa.Enum("INFO","LOW","MEDIUM","HIGH","CRITICAL",name="incidentseverity")
    status=sa.Enum("OPEN","RESOLVED",name="incidentstatus")
    op.create_table("incidents",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("public_id",sa.String(30),unique=True),sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id")),sa.Column("scope",sa.String(10),nullable=False),sa.Column("incident_type",sa.String(40),nullable=False),sa.Column("severity",severity,nullable=False),sa.Column("status",status,nullable=False),sa.Column("title",sa.String(200),nullable=False),sa.Column("summary",sa.String(1000),nullable=False),sa.Column("source",sa.String(40),nullable=False),sa.Column("fingerprint",sa.String(64),nullable=False),sa.Column("started_at",sa.DateTime(timezone=True),nullable=False),sa.Column("last_updated_at",sa.DateTime(timezone=True),nullable=False),sa.Column("resolved_at",sa.DateTime(timezone=True)),sa.Column("resolution",sa.String(30)),sa.Column("current_instance_id",sa.String(41)),sa.Column("context",sa.JSON(),nullable=False),sa.Column("error_group_id",sa.Integer()))
    op.create_table("error_groups",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("public_id",sa.String(30),unique=True),sa.Column("bot_id",sa.String(36),sa.ForeignKey("bots.id"),nullable=False),sa.Column("fingerprint",sa.String(64),nullable=False),sa.Column("exception_type",sa.String(100)),sa.Column("safe_signature",sa.String(500),nullable=False),sa.Column("source",sa.String(80),nullable=False),sa.Column("first_seen",sa.DateTime(timezone=True),nullable=False),sa.Column("last_seen",sa.DateTime(timezone=True),nullable=False),sa.Column("occurrence_count",sa.Integer(),nullable=False),sa.Column("latest_incident_id",sa.Integer(),sa.ForeignKey("incidents.id")),sa.UniqueConstraint("bot_id","fingerprint",name="uq_error_group_bot_fingerprint"))
    op.create_table("incident_events",sa.Column("id",sa.Integer(),primary_key=True),sa.Column("incident_id",sa.Integer(),sa.ForeignKey("incidents.id"),nullable=False),sa.Column("source_event_id",sa.Integer(),sa.ForeignKey("activity_events.id")),sa.Column("source_key",sa.String(36),nullable=False),sa.Column("timestamp",sa.DateTime(timezone=True),nullable=False),sa.Column("event_code",sa.String(100),nullable=False),sa.Column("label",sa.String(200),nullable=False),sa.Column("detail",sa.String(1000)),sa.Column("metadata_snapshot",sa.JSON(),nullable=False),sa.UniqueConstraint("source_key","event_code",name="uq_incident_source_key"))
    for table,columns in {"incidents":["public_id","bot_id","scope","incident_type","severity","status","fingerprint","started_at"],"error_groups":["public_id","bot_id","fingerprint"],"incident_events":["incident_id","timestamp"]}.items():
        for column in columns: op.create_index(f"ix_{table}_{column}",table,[column])
def downgrade():
    op.drop_table("incident_events"); op.drop_table("error_groups"); op.drop_table("incidents")
