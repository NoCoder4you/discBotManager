"""Trusted, read-only operational incident projection over structured events."""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from pathlib import PurePath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.events import DomainEvent, EventType
from app.models import (ActivityEvent, BotInstance, ErrorGroup, Incident, IncidentEvent,
                        IncidentSeverity, IncidentStatus, utcnow)
from app.services.console import SecretRedactor

_REDACTOR = SecretRedactor([])
_VOLATILE = re.compile(r"\b(?:0x[0-9a-f]+|[0-9a-f]{8}-[0-9a-f-]{27,}|\d{4}-\d\d-\d\d[T ][\d:.+Z-]+)\b", re.I)
_EXCEPTION = re.compile(r"(?m)^([A-Za-z_][\w.]*(?:Error|Exception|Warning)|KeyError|RuntimeError):\s*(.+)$")

INCIDENT_RULES = {
    EventType.BOT_CRASHED:("BOT_CRASH","HIGH","Bot process crashed","process"),
    EventType.BOT_PROCESS_LOST:("BOT_CRASH","HIGH","Bot process exited unexpectedly","process"),
    EventType.BOT_PROCESS_FAILED:("READY_TIMEOUT","HIGH","Bot failed to start or become ready","process"),
    EventType.BOT_DISCONNECTED:("DISCORD_DISCONNECT","MEDIUM","Discord connection lost","heartbeat"),
    EventType.BOT_HEARTBEAT_LOST:("DISCORD_DISCONNECT","MEDIUM","Discord heartbeat timed out","heartbeat"),
    EventType.SUPERVISOR_DISCONNECTED:("SUPERVISOR_UNAVAILABLE","HIGH","Supervisor unavailable","supervisor"),
    EventType.TASK_FAILED:("TASK_FAILURE","MEDIUM","Scheduled task failed","scheduler"),
    EventType.TASK_TIMED_OUT:("TASK_TIMEOUT","HIGH","Scheduled task timed out","scheduler"),
    EventType.DATABASE_EDIT_FAILED:("DATABASE_EDIT_FAILURE","HIGH","Database edit failed","database"),
    EventType.DATABASE_EDIT_RECOVERY_FAILED:("DATABASE_RECOVERY_FAILURE","CRITICAL","Database recovery failed","database"),
    EventType.DATABASE_EDIT_READY_TIMEOUT:("READY_TIMEOUT","HIGH","Bot did not become ready after database edit","database"),
    EventType.BACKUP_VERIFICATION_FAILED:("BACKUP_FAILURE","HIGH","Backup verification failed","backup"),
    EventType.BACKUP_RESTORE_FAILED:("RESTORE_FAILURE","CRITICAL","Backup restore failed","backup"),
}
RECOVERY_RULES = {
    EventType.BOT_READY:("DISCORD_DISCONNECT","READY_TIMEOUT","BOT_CRASH"),
    EventType.BOT_READY_RECOVERED:("DISCORD_DISCONNECT","READY_TIMEOUT","BOT_CRASH"),
    EventType.BOT_HEARTBEAT_RECOVERED:("DISCORD_DISCONNECT",),
    EventType.SUPERVISOR_CONNECTED:("SUPERVISOR_UNAVAILABLE",),
}
POINT_IN_TIME = {"TASK_FAILURE","TASK_TIMEOUT","DATABASE_EDIT_FAILURE","DATABASE_RECOVERY_FAILURE","BACKUP_FAILURE","RESTORE_FAILURE"}
LABELS = {event.value:title for event,(_,_,title,_) in INCIDENT_RULES.items()}
LABELS.update({"BOT_READY":"Discord Ready restored","BOT_READY_RECOVERED":"Discord Ready restored","BOT_HEARTBEAT_RECOVERED":"Discord heartbeat restored","SUPERVISOR_CONNECTED":"Supervisor connectivity restored","CRASH_LOOP":"Crash loop detected"})
SAFE_CONTEXT = {"instance_id","pid","exit_code","uptime_seconds","last_heartbeat_at","discord_ready","cpu_percent","rss_bytes","task_id","task_name","run_id","trigger","duration_ms","status","summary","operation_id","backup_id","database_id","table","rollback_succeeded","process_running","console_excerpt","exception","source"}

def safe_text(value:object, limit:int=1000)->str:
    text=_REDACTOR.redact(str(value))
    text=re.sub(r'(?m)(?:/[^\s/:]+)+/([^/\s:]+\.py)(?=:\d+)',r'…/\1',text)
    return text.encode("utf-8")[:limit].decode("utf-8","ignore")

def error_signature(payload:dict)->tuple[str|None,str]:
    raw=safe_text(payload.get("traceback") or payload.get("error") or payload.get("summary") or "Operational failure",32768)
    match=_EXCEPTION.search(raw); exception=match.group(1).split(".")[-1] if match else None
    tail=(match.group(2) if match else raw.splitlines()[-1] if raw else "Operational failure")
    tail=_VOLATILE.sub("?",tail); tail=re.sub(r"\b\d+\b","#",tail)
    frames=re.findall(r'File "([^"]+\.py)", line \d+, in ([\w<>]+)',raw)
    frame=f"{PurePath(frames[-1][0]).name}:{frames[-1][1]}" if frames else ""
    return exception,safe_text(" · ".join(x for x in (exception,tail,frame) if x),500)

class IncidentService:
    """Consumes a fixed event allow-list. It observes state and never performs recovery."""
    def __init__(self,db:Session): self.db=db
    def process(self,event:DomainEvent,activity:ActivityEvent)->Incident|None:
        if event.type in RECOVERY_RULES:
            return self._resolve(event,activity,RECOVERY_RULES[event.type])
        rule=INCIDENT_RULES.get(event.type)
        if not rule: return None
        kind,severity,title,source=rule
        if self.db.scalar(select(IncidentEvent.id).where(IncidentEvent.source_key==event.event_id,IncidentEvent.event_code==event.type.value)): return None
        context={key:self._safe_value(value) for key,value in event.payload.items() if key in SAFE_CONTEXT and value is not None}
        exception,signature=error_signature(event.payload)
        correlation=str(event.payload.get("task_id") or event.payload.get("instance_id") or event.payload.get("operation_id") or "")
        fingerprint=hashlib.sha256(f"{event.bot_id}|{kind}|{signature}|{correlation}".encode()).hexdigest()
        group=None
        if exception or event.payload.get("error") or event.payload.get("traceback"):
            group=self._group(event.bot_id,fingerprint,exception,signature,source,event.timestamp)
        incident=Incident(bot_id=event.bot_id,scope="BOT" if event.bot_id else "PLATFORM",incident_type=kind,severity=IncidentSeverity[severity],status=IncidentStatus.OPEN,title=title,summary=self._summary(title,event.payload),source=source,fingerprint=fingerprint,started_at=event.timestamp,last_updated_at=event.timestamp,current_instance_id=context.get("instance_id"),context=context,error_group_id=group.id if group else None)
        self.db.add(incident); self.db.flush(); incident.public_id=f"INC-{incident.id:06d}"
        self._timeline(incident,activity,event.type.value,title,event.payload,event.event_id)
        if group: group.latest_incident_id=incident.id
        if kind=="BOT_CRASH": self._promote_crash_loop(incident,activity)
        if kind in POINT_IN_TIME:
            incident.status=IncidentStatus.RESOLVED; incident.resolved_at=event.timestamp; incident.resolution="OCCURRED"
        return incident
    def reconcile(self)->int:
        """Resolve stale connection incidents from durable current instance state."""
        resolved=0
        rows=self.db.scalars(select(Incident).where(Incident.status==IncidentStatus.OPEN,Incident.incident_type.in_(("DISCORD_DISCONNECT","READY_TIMEOUT","BOT_CRASH")),Incident.bot_id.is_not(None))).all()
        for incident in rows:
            instance=self.db.scalar(select(BotInstance).where(BotInstance.bot_id==incident.bot_id).order_by(BotInstance.started_at.desc(),BotInstance.id.desc()))
            if instance and instance.discord_ready and instance.expected_running:
                incident.status=IncidentStatus.RESOLVED; incident.resolved_at=utcnow(); incident.last_updated_at=incident.resolved_at; incident.resolution="RECONCILED"; resolved+=1
        return resolved
    def _resolve(self,event,activity,kinds):
        incident=self.db.scalar(select(Incident).where(Incident.status==IncidentStatus.OPEN,Incident.incident_type.in_(kinds),Incident.bot_id==event.bot_id).order_by(Incident.started_at.desc()))
        if not incident: return None
        self._timeline(incident,activity,event.type.value,LABELS[event.type.value],event.payload,event.event_id)
        incident.status=IncidentStatus.RESOLVED; incident.resolved_at=event.timestamp; incident.last_updated_at=event.timestamp; incident.resolution="AUTOMATIC"; return incident
    def _timeline(self,incident,activity,code,label,payload,source_key):
        if self.db.scalar(select(IncidentEvent.id).where(IncidentEvent.source_key==source_key,IncidentEvent.event_code==code)): return
        detail=safe_text(payload.get("summary") or payload.get("error") or "",1000) or None
        snapshot={k:self._safe_value(v) for k,v in payload.items() if k in SAFE_CONTEXT and k!="console_excerpt"}
        self.db.add(IncidentEvent(incident_id=incident.id,source_event_id=activity.id,source_key=source_key,timestamp=activity.timestamp,event_code=code,label=label,detail=detail,metadata_snapshot=snapshot))
    def _group(self,bot_id,fingerprint,exception,signature,source,when):
        group=self.db.scalar(select(ErrorGroup).where(ErrorGroup.bot_id==bot_id,ErrorGroup.fingerprint==fingerprint))
        if group: group.occurrence_count+=1; group.last_seen=when
        else:
            group=ErrorGroup(bot_id=bot_id,fingerprint=fingerprint,exception_type=exception,safe_signature=signature,source=source,first_seen=when,last_seen=when); self.db.add(group); self.db.flush(); group.public_id=f"ERR-{group.id:06d}"
        return group
    def _promote_crash_loop(self,incident,activity):
        since=activity.timestamp-timedelta(minutes=5)
        recent=self.db.scalar(select(func.count(Incident.id)).where(Incident.bot_id==incident.bot_id,Incident.incident_type.in_(("BOT_CRASH","CRASH_LOOP")),Incident.started_at>=since)) or 0
        if recent>=3:
            incident.incident_type="CRASH_LOOP"; incident.severity=IncidentSeverity.CRITICAL; incident.title="Bot entered a crash loop"; incident.summary="The bot process crashed at least three times within five minutes."
    @staticmethod
    def _summary(title,payload):
        identifier=payload.get("task_name") or payload.get("task_id") or payload.get("run_id")
        return safe_text(f"{title}{' ('+str(identifier)+')' if identifier else ''}. The platform recorded this incident without taking operational action.")
    @staticmethod
    def _safe_value(value):
        if isinstance(value,(str,int,float,bool)): return safe_text(value,32768) if isinstance(value,str) else value
        return safe_text(value,1000)
