from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable
from sqlalchemy.orm import Session
from app.models import ActivityEvent, AuditLog, User
class EventType(str,Enum):
    USER_LOGIN="USER_LOGIN"; USER_LOGOUT="USER_LOGOUT"; USER_ACCESS_CHANGED="USER_ACCESS_CHANGED"; BOT_REGISTERED="BOT_REGISTERED"; BOT_CONFIGURATION_CHANGED="BOT_CONFIGURATION_CHANGED"; BOT_ASSIGNMENT_CHANGED="BOT_ASSIGNMENT_CHANGED"; BOT_STARTED="BOT_STARTED"; BOT_STOPPED="BOT_STOPPED"; BOT_CRASHED="BOT_CRASHED"; BOT_RESTARTED="BOT_RESTARTED"; COG_RELOADED="COG_RELOADED"; CONFIG_CHANGED="CONFIG_CHANGED"; FILE_CHANGED="FILE_CHANGED"; BACKUP_CREATED="BACKUP_CREATED"; BACKUP_RESTORED="BACKUP_RESTORED"; PERMISSION_CHANGED="PERMISSION_CHANGED"
    BOT_START_REQUESTED="BOT_START_REQUESTED"; BOT_STOP_REQUESTED="BOT_STOP_REQUESTED"; BOT_RESTART_REQUESTED="BOT_RESTART_REQUESTED"; BOT_PROCESS_FAILED="BOT_PROCESS_FAILED"
    BOT_PROCESS_DISCOVERED="BOT_PROCESS_DISCOVERED"; BOT_PROCESS_ADOPTED="BOT_PROCESS_ADOPTED"; BOT_PROCESS_LOST="BOT_PROCESS_LOST"; BOT_RECONCILED="BOT_RECONCILED"; SUPERVISOR_CONNECTED="SUPERVISOR_CONNECTED"; SUPERVISOR_DISCONNECTED="SUPERVISOR_DISCONNECTED"
@dataclass(frozen=True)
class DomainEvent:
    type:EventType; actor:User|None=None; bot_id:str|None=None; payload:dict[str,Any]=field(default_factory=dict); timestamp:datetime=field(default_factory=lambda:datetime.now(timezone.utc))
class EventBus:
    """Synchronous transaction-scoped publisher; consumers can be replaced later."""
    def __init__(self,db:Session): self.db=db; self.consumers:list[Callable[[DomainEvent],None]]=[]
    def publish(self,event:DomainEvent)->None:
        self.db.add(ActivityEvent(timestamp=event.timestamp,event_type=event.type.value,actor_id=event.actor.id if event.actor else None,bot_id=event.bot_id,payload=event.payload))
        for consumer in self.consumers: consumer(event)
class AuditService:
    """The service intentionally exposes append only; no update/delete API exists."""
    def __init__(self,db:Session): self.db=db
    def record(self,event:DomainEvent,result:str,target:str|None=None,operation_id:str|None=None)->AuditLog:
        row=AuditLog(timestamp=event.timestamp,discord_user_id=event.actor.discord_id if event.actor else None,user_display=event.actor.display_name if event.actor else None,bot_id=event.bot_id,action=event.type.value,target=target,result=result,event_metadata=event.payload,operation_id=operation_id); self.db.add(row); return row
