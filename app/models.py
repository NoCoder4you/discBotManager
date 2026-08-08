from __future__ import annotations
import enum, uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

def utcnow(): return datetime.now(timezone.utc)
class PlatformRole(str, enum.Enum): OWNER="owner"; ADMINISTRATOR="administrator"; OPERATOR="operator"; VIEWER="viewer"
class Effect(str, enum.Enum): GRANT="grant"; DENY="deny"
class OperationStatus(str, enum.Enum): QUEUED="queued"; RUNNING="running"; COMPLETED="completed"; FAILED="failed"
class User(Base):
    __tablename__="users"; id: Mapped[int]=mapped_column(primary_key=True); discord_id: Mapped[str]=mapped_column(String(32),unique=True,index=True); username: Mapped[str]=mapped_column(String(100)); display_name: Mapped[str]=mapped_column(String(100)); avatar: Mapped[str|None]=mapped_column(String(255)); platform_role: Mapped[PlatformRole]=mapped_column(Enum(PlatformRole),default=PlatformRole.VIEWER); enabled: Mapped[bool]=mapped_column(Boolean,default=True); created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow); last_login: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
class Bot(Base):
    __tablename__="bots"; id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid.uuid4())); display_name: Mapped[str]=mapped_column(String(100)); description: Mapped[str]=mapped_column(Text,default=""); folder: Mapped[str]=mapped_column(String(500)); entry_file: Mapped[str]=mapped_column(String(255)); python_executable: Mapped[str]=mapped_column(String(500),default="python"); accent_colour: Mapped[str]=mapped_column(String(7),default="#5865f2"); enabled: Mapped[bool]=mapped_column(Boolean,default=True); owner_id: Mapped[int|None]=mapped_column(ForeignKey("users.id")); auto_restart: Mapped[bool]=mapped_column(Boolean,default=False); adapter: Mapped[str]=mapped_column(String(200),default="base"); modules: Mapped[list]=mapped_column(JSON,default=list); data_roots: Mapped[list]=mapped_column(JSON,default=list); backup_roots: Mapped[list]=mapped_column(JSON,default=list); management_secret_hash: Mapped[str|None]=mapped_column(String(64),nullable=True)
class Role(Base):
    __tablename__="roles"; id: Mapped[int]=mapped_column(primary_key=True); key: Mapped[str]=mapped_column(String(80),unique=True); name: Mapped[str]=mapped_column(String(100)); scope: Mapped[str]=mapped_column(String(20),default="bot")
class Permission(Base):
    __tablename__="permissions"; id: Mapped[int]=mapped_column(primary_key=True); key: Mapped[str]=mapped_column(String(100),unique=True); description: Mapped[str]=mapped_column(String(255),default="")
class RolePermission(Base):
    __tablename__="role_permissions"; role_id: Mapped[int]=mapped_column(ForeignKey("roles.id"),primary_key=True); permission_id: Mapped[int]=mapped_column(ForeignKey("permissions.id"),primary_key=True)
class BotAssignment(Base):
    __tablename__="bot_assignments"; __table_args__=(UniqueConstraint("user_id","bot_id"),); id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int]=mapped_column(ForeignKey("users.id"),index=True); bot_id: Mapped[str]=mapped_column(ForeignKey("bots.id"),index=True); role_id: Mapped[int]=mapped_column(ForeignKey("roles.id")); enabled: Mapped[bool]=mapped_column(Boolean,default=True); created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow); updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,onupdate=utcnow); role: Mapped[Role]=relationship(); bot: Mapped[Bot]=relationship()
class UserPermission(Base):
    __tablename__="user_permissions"; id: Mapped[int]=mapped_column(primary_key=True); user_id: Mapped[int]=mapped_column(ForeignKey("users.id")); bot_id: Mapped[str|None]=mapped_column(ForeignKey("bots.id")); permission_id: Mapped[int]=mapped_column(ForeignKey("permissions.id")); effect: Mapped[Effect]=mapped_column(Enum(Effect)); permission: Mapped[Permission]=relationship()
class Session(Base):
    __tablename__="sessions"; id: Mapped[str]=mapped_column(String(64),primary_key=True); user_id: Mapped[int|None]=mapped_column(ForeignKey("users.id")); oauth_state: Mapped[str|None]=mapped_column(String(128)); csrf_token: Mapped[str]=mapped_column(String(128)); expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True)); user: Mapped[User|None]=relationship()
class Operation(Base):
    __tablename__="operations"; id: Mapped[int]=mapped_column(primary_key=True); public_id: Mapped[str]=mapped_column(String(30),unique=True); kind: Mapped[str]=mapped_column(String(30)); status: Mapped[OperationStatus]=mapped_column(Enum(OperationStatus),default=OperationStatus.QUEUED); user_id: Mapped[int|None]=mapped_column(ForeignKey("users.id")); bot_id: Mapped[str|None]=mapped_column(ForeignKey("bots.id")); created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow); completed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); event_metadata: Mapped[dict]=mapped_column(JSON,default=dict); error: Mapped[str|None]=mapped_column(String(255))
class AuditLog(Base):
    __tablename__="audit_log"; id: Mapped[int]=mapped_column(primary_key=True); timestamp: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,index=True); discord_user_id: Mapped[str|None]=mapped_column(String(32)); user_display: Mapped[str|None]=mapped_column(String(100)); bot_id: Mapped[str|None]=mapped_column(ForeignKey("bots.id")); action: Mapped[str]=mapped_column(String(100)); target: Mapped[str|None]=mapped_column(String(255)); result: Mapped[str]=mapped_column(String(30)); event_metadata: Mapped[dict]=mapped_column(JSON,default=dict); operation_id: Mapped[str|None]=mapped_column(String(30))
class ActivityEvent(Base):
    __tablename__="activity_events"; id: Mapped[int]=mapped_column(primary_key=True); timestamp: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,index=True); event_type: Mapped[str]=mapped_column(String(100)); actor_id: Mapped[int|None]=mapped_column(ForeignKey("users.id")); bot_id: Mapped[str|None]=mapped_column(ForeignKey("bots.id")); payload: Mapped[dict]=mapped_column(JSON,default=dict)
class BotInstance(Base):
    """Durable identity for one generation of a registered bot process."""
    __tablename__="bot_instances"
    id: Mapped[int]=mapped_column(primary_key=True)
    bot_id: Mapped[str]=mapped_column(ForeignKey("bots.id"),index=True)
    instance_id: Mapped[str]=mapped_column(String(41),unique=True,index=True)
    pid: Mapped[int|None]=mapped_column(Integer,index=True)
    process_created_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int|None]=mapped_column(Integer)
    expected_running: Mapped[bool]=mapped_column(Boolean,default=False,index=True)
    state: Mapped[str]=mapped_column(String(20),default="offline")
    python_executable: Mapped[str]=mapped_column(String(500))
    entry_file: Mapped[str]=mapped_column(String(500))
    working_directory: Mapped[str]=mapped_column(String(500))
    supervisor_instance_id: Mapped[str|None]=mapped_column(String(41))
    discord_connected: Mapped[bool]=mapped_column(Boolean,default=False)
    discord_ready: Mapped[bool]=mapped_column(Boolean,default=False)
    last_heartbeat_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_agent_timestamp: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_ready_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_disconnect_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    discord_latency_ms: Mapped[float|None]=mapped_column()
    guild_count: Mapped[int|None]=mapped_column(Integer)
