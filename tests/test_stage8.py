from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.core.events import DomainEvent, EventBus, EventType
from app.models import Bot, ErrorGroup, Incident, IncidentEvent, IncidentSeverity, IncidentStatus

def bot(db,name="Events"):
    row=Bot(display_name=name,folder="/tmp",entry_file="bot.py"); db.add(row); db.flush(); return row

def test_crash_creation_context_and_ready_resolution(db):
    b=bot(db); now=datetime.now(timezone.utc)
    EventBus(db).publish(DomainEvent(EventType.BOT_CRASHED,None,b.id,{"instance_id":"INST-1","exit_code":1,"uptime_seconds":42,"cpu_percent":2.1,"rss_bytes":1234},now))
    row=db.scalar(select(Incident)); assert row.public_id=="INC-000001" and row.incident_type=="BOT_CRASH" and row.severity is IncidentSeverity.HIGH and row.status is IncidentStatus.OPEN
    assert row.context["exit_code"]==1 and row.current_instance_id=="INST-1"
    EventBus(db).publish(DomainEvent(EventType.BOT_READY,None,b.id,{"instance_id":"INST-2"},now+timedelta(seconds=10)))
    assert row.status is IncidentStatus.RESOLVED and row.resolution=="AUTOMATIC" and len(list(db.scalars(select(IncidentEvent))))==2

def test_disconnect_duplicate_and_recovery_are_idempotent(db):
    b=bot(db); event=DomainEvent(EventType.BOT_HEARTBEAT_LOST,None,b.id,{"instance_id":"I"})
    bus=EventBus(db); bus.publish(event); bus.publish(event); bus.publish(DomainEvent(EventType.BOT_HEARTBEAT_RECOVERED,None,b.id,{}))
    assert len(list(db.scalars(select(Incident))))==1
    assert db.scalar(select(Incident)).status is IncidentStatus.RESOLVED

def test_grouped_errors_redact_and_distinguish(db):
    b=bot(db); payload={"task_id":"weekly","error":"KeyError: weekly_points token=abc.def.ghi"}
    EventBus(db).publish(DomainEvent(EventType.TASK_FAILED,None,b.id,payload)); EventBus(db).publish(DomainEvent(EventType.TASK_FAILED,None,b.id,payload))
    groups=list(db.scalars(select(ErrorGroup))); assert len(groups)==1 and groups[0].occurrence_count==2 and "abc.def.ghi" not in groups[0].safe_signature and "[REDACTED]" in groups[0].safe_signature
    EventBus(db).publish(DomainEvent(EventType.TASK_FAILED,None,b.id,{"task_id":"weekly","error":"ValueError: other"}))
    assert len(list(db.scalars(select(ErrorGroup))))==2

def test_crash_loop_and_point_failure_policy(db):
    b=bot(db); now=datetime.now(timezone.utc)
    for offset in range(3): EventBus(db).publish(DomainEvent(EventType.BOT_CRASHED,None,b.id,{"instance_id":f"I{offset}"},now+timedelta(seconds=offset)))
    latest=db.scalar(select(Incident).order_by(Incident.id.desc())); assert latest.incident_type=="CRASH_LOOP" and latest.severity is IncidentSeverity.CRITICAL
    EventBus(db).publish(DomainEvent(EventType.DATABASE_EDIT_RECOVERY_FAILED,None,b.id,{"operation_id":"ACT-1","backup_id":"BKP-1","secret_row":"hidden"}))
    latest=db.scalar(select(Incident).order_by(Incident.id.desc())); assert latest.status is IncidentStatus.RESOLVED and latest.resolution=="OCCURRED" and "secret_row" not in latest.context
