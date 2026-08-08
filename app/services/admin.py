from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.models import Bot, BotAssignment, Effect, OperationStatus, Permission, PlatformRole, Role, User, UserPermission, utcnow
from app.schemas import AssignmentMutation, BotMutation, PermissionOverrideMutation
from app.services.catalog import ROLE_MAPPINGS

class AdminError(ValueError): pass

def _event(db, actor, event_type, bot_id, payload, target):
    op=create_operation(db,"activity",user_id=actor.id,bot_id=bot_id,event_metadata=payload)
    event=DomainEvent(event_type,actor,bot_id,payload); EventBus(db).publish(event); AuditService(db).record(event,"success",target,op.public_id)
    op.status=OperationStatus.COMPLETED; op.completed_at=utcnow(); return op

class AdminService:
    def __init__(self,db:Session): self.db=db
    def set_user_enabled(self,actor:User,target:User,enabled:bool):
        if target.platform_role is PlatformRole.OWNER: raise AdminError("The Platform Owner cannot be disabled")
        before=target.enabled; target.enabled=enabled
        op=_event(self.db,actor,EventType.USER_ACCESS_CHANGED,None,{"before":{"enabled":before},"after":{"enabled":enabled}},target.discord_id); self.db.commit(); return op
    def validate_paths(self,data:BotMutation):
        root=Path(get_settings().bot_root).resolve(); folder=Path(data.folder)
        folder=(root/folder).resolve() if not folder.is_absolute() else folder.resolve()
        if not folder.is_relative_to(root) or not folder.is_dir(): raise AdminError("Bot folder must be an existing directory within BOT_ROOT")
        entry=(folder/data.entry_file).resolve()
        if not entry.is_relative_to(folder) or not entry.is_file(): raise AdminError("Entry file must exist within the bot folder")
        executable=Path(data.python_executable).resolve()
        if not executable.is_file(): raise AdminError("Python executable must be an existing file")
        data_root=(folder/data.data_root).resolve()
        if Path(data.data_root).is_absolute() or not data_root.is_relative_to(folder) or not data_root.is_dir(): raise AdminError("Data root must be an existing directory within the bot folder")
        return folder,entry,executable,data_root
    def create_bot(self,actor:User,data:BotMutation):
        if self.db.get(Bot,data.id): raise AdminError("Bot ID is already registered")
        folder,entry,executable,data_root=self.validate_paths(data)
        bot=Bot(id=data.id,display_name=data.display_name,description=data.description,folder=str(folder),entry_file=str(entry.relative_to(folder)),python_executable=str(executable),accent_colour=data.accent_colour,enabled=data.enabled,owner_id=actor.id,adapter=data.adapter,data_roots=[str(data_root.relative_to(folder))],backup_include=[x.strip() for x in data.backup_include.splitlines() if x.strip()],backup_exclude=[x.strip() for x in data.backup_exclude.splitlines() if x.strip()],restore_policy=data.restore_policy)
        self.db.add(bot); self.db.flush(); op=_event(self.db,actor,EventType.BOT_REGISTERED,bot.id,{"after":{"display_name":bot.display_name,"enabled":bot.enabled,"adapter":bot.adapter}},bot.id); self.db.commit(); return bot,op
    def update_bot(self,actor:User,bot:Bot,data:BotMutation):
        if data.id != bot.id: raise AdminError("Internal bot ID cannot be changed")
        folder,entry,executable,data_root=self.validate_paths(data)
        before={"display_name":bot.display_name,"description":bot.description,"enabled":bot.enabled,"adapter":bot.adapter}
        bot.display_name=data.display_name; bot.description=data.description; bot.folder=str(folder); bot.entry_file=str(entry.relative_to(folder)); bot.python_executable=str(executable); bot.accent_colour=data.accent_colour; bot.enabled=data.enabled; bot.adapter=data.adapter; bot.data_roots=[str(data_root.relative_to(folder))]; bot.backup_include=[x.strip() for x in data.backup_include.splitlines() if x.strip()]; bot.backup_exclude=[x.strip() for x in data.backup_exclude.splitlines() if x.strip()]; bot.restore_policy=data.restore_policy
        after={"display_name":bot.display_name,"description":bot.description,"enabled":bot.enabled,"adapter":bot.adapter}
        op=_event(self.db,actor,EventType.BOT_CONFIGURATION_CHANGED,bot.id,{"before":before,"after":after},bot.id); self.db.commit(); return bot,op
    def assign(self,actor:User,target:User,data:AssignmentMutation):
        bot=self.db.get(Bot,data.bot_id); role=self.db.scalar(select(Role).where(Role.key==data.role_key,Role.scope=="bot"))
        if not bot or not role or data.role_key not in ROLE_MAPPINGS: raise AdminError("Unknown bot or canonical role")
        row=self.db.scalar(select(BotAssignment).where(BotAssignment.user_id==target.id,BotAssignment.bot_id==bot.id)); before=None
        if row: before={"role":row.role.key,"enabled":row.enabled}; row.role_id=role.id; row.enabled=data.enabled
        else: row=BotAssignment(user_id=target.id,bot_id=bot.id,role_id=role.id,enabled=data.enabled); self.db.add(row)
        payload={"before":before,"after":{"role":role.key,"enabled":data.enabled}}
        op=_event(self.db,actor,EventType.BOT_ASSIGNMENT_CHANGED,bot.id,payload,target.discord_id); self.db.commit(); return row,op
    def revoke(self,actor:User,target:User,bot_id:str):
        row=self.db.scalar(select(BotAssignment).where(BotAssignment.user_id==target.id,BotAssignment.bot_id==bot_id))
        if not row: raise AdminError("Assignment not found")
        self.db.delete(row); op=_event(self.db,actor,EventType.BOT_ASSIGNMENT_CHANGED,bot_id,{"before":{"role":row.role.key,"enabled":row.enabled},"after":None},target.discord_id); self.db.commit(); return op
    def override(self,actor:User,target:User,bot_id:str,data:PermissionOverrideMutation):
        permission=self.db.scalar(select(Permission).where(Permission.key==data.permission_key))
        assignment=self.db.scalar(select(BotAssignment).where(BotAssignment.user_id==target.id,BotAssignment.bot_id==bot_id))
        if not permission or not assignment: raise AdminError("Unknown permission or assignment")
        rows=list(self.db.scalars(select(UserPermission).where(UserPermission.user_id==target.id,UserPermission.bot_id==bot_id,UserPermission.permission_id==permission.id)))
        before=rows[0].effect.value if rows else "inherit"
        for row in rows: self.db.delete(row)
        if data.state!="inherit": self.db.add(UserPermission(user_id=target.id,bot_id=bot_id,permission_id=permission.id,effect=Effect.GRANT if data.state=="allow" else Effect.DENY))
        payload={"permission":permission.key,"before":before,"after":data.state}; op=_event(self.db,actor,EventType.PERMISSION_CHANGED,bot_id,payload,target.discord_id); self.db.commit(); return op
