from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import select
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.models import BotMaintenance, OperationStatus, utcnow

DEFAULT_MESSAGE="This bot is currently unavailable while maintenance is being carried out. Please try again later."

class MaintenanceRequired(RuntimeError): pass

class MaintenanceService:
    """Authoritative durable maintenance transitions and applied-state reconciliation."""
    def __init__(self,db): self.db=db
    def get(self,bot_id):
        return self.db.get(BotMaintenance,bot_id) or BotMaintenance(bot_id=bot_id,enabled=False)
    @staticmethod
    def sync_status(row,process_running=True):
        if not process_running:return "PENDING_SYNC" if row.enabled else "INACTIVE"
        if row.sync_error:return "DEGRADED"
        return "ACTIVE" if row.applied_enabled is row.enabled else "PENDING_SYNC"
    def payload(self,row,process_running=True):
        end=row.planned_end_at
        if end and end.tzinfo is None:end=end.replace(tzinfo=timezone.utc)
        return {"desired_enabled":row.enabled,"applied_enabled":row.applied_enabled,"sync_status":self.sync_status(row,process_running),"reason":row.reason,"public_message":row.public_message or DEFAULT_MESSAGE,"enabled_at":row.enabled_at,"enabled_by":row.enabled_by.display_name if row.enabled_by else None,"planned_end_at":row.planned_end_at,"planned_end_passed":bool(row.enabled and end and end<datetime.now(timezone.utc)),"updated_at":row.updated_at,"bypass_user_ids":list(row.bypass_user_ids or []),"bypass_roles":list(row.bypass_roles or [])}
    def set(self,bot,user,enabled,reason=None,public_message=None,planned_end_at=None):
        row=self.db.get(BotMaintenance,bot.id)
        def same_time(left,right):
            if left is None or right is None:return left is right
            if left.tzinfo is None:left=left.replace(tzinfo=timezone.utc)
            if right.tzinfo is None:right=right.replace(tzinfo=timezone.utc)
            return left==right
        if row and row.enabled is enabled:
            if not enabled or (row.reason==reason and row.public_message==public_message and same_time(row.planned_end_at,planned_end_at)): return row,None
        operation=create_operation(self.db,"activity",user_id=user.id,bot_id=bot.id,event_metadata={"maintenance":enabled})
        row=row or BotMaintenance(bot_id=bot.id,bypass_user_ids=[],bypass_roles=[]); self.db.add(row)
        row.enabled=enabled; row.updated_at=utcnow(); row.sync_error=None
        if enabled:
            row.reason=reason; row.public_message=public_message; row.planned_end_at=planned_end_at; row.enabled_at=utcnow(); row.enabled_by_id=user.id
        row.applied_enabled=None; row.applied_instance_id=None; row.applied_at=None
        event_type=EventType.BOT_MAINTENANCE_ENABLED if enabled else EventType.BOT_MAINTENANCE_DISABLED
        payload={"source":user.display_name,"operation_id":operation.public_id,"reason":reason if enabled else row.reason,"public_message":public_message if enabled else row.public_message,"planned_end_at":planned_end_at.isoformat() if planned_end_at else None,"result":"PENDING_SYNC"}
        event=DomainEvent(event_type,user,bot.id,payload); EventBus(self.db).publish(event); AuditService(self.db).record(event,"success",bot.display_name,operation.public_id)
        operation.status=OperationStatus.COMPLETED; operation.completed_at=utcnow(); self.db.commit(); self.db.refresh(row); return row,operation
    def require_maintenance(self,bot_id):
        row=self.db.get(BotMaintenance,bot_id)
        if not row or not row.enabled: raise MaintenanceRequired("This operation requires Maintenance Mode.")
        return row
    def reconcile_applied(self,bot_id,instance_id,applied):
        row=self.db.get(BotMaintenance,bot_id)
        if not row: row=BotMaintenance(bot_id=bot_id,enabled=False,bypass_user_ids=[],bypass_roles=[]); self.db.add(row)
        transition=row.applied_enabled is not applied or row.applied_instance_id!=instance_id
        row.applied_enabled=applied; row.applied_instance_id=instance_id; row.applied_at=utcnow(); row.sync_error=None
        if transition and applied is row.enabled: EventBus(self.db).publish(DomainEvent(EventType.BOT_MAINTENANCE_SYNCED,None,bot_id,{"source":"SYSTEM","instance_id":instance_id,"enabled":applied}))
        self.db.commit(); return row
