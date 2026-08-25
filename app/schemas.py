import re
from typing import Literal
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator

BOT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,35}$")

class UserStatusUpdate(BaseModel): enabled: bool
class AssignmentMutation(BaseModel):
    bot_id: str = Field(min_length=2, max_length=36)
    role_key: Literal["viewer", "operator", "administrator"]
    enabled: bool = True
class PermissionOverrideMutation(BaseModel):
    permission_key: str = Field(min_length=3, max_length=100)
    state: Literal["inherit", "allow", "deny"]
class ProcessAction(BaseModel): action: Literal["start", "stop", "restart"]
class EnableMaintenanceRequest(BaseModel):
    reason:str=Field(min_length=1,max_length=500)
    public_message:str|None=Field(default=None,max_length=1000)
    planned_end_at:datetime|None=None
    @field_validator("reason","public_message")
    @classmethod
    def maintenance_text(cls,value):
        if value is None:return None
        value=value.strip()
        if "<" in value or ">" in value: raise ValueError("Maintenance text must be plain text")
        return value or None
class DisableMaintenanceRequest(BaseModel): pass
class CreateBackup(BaseModel):
    reason: str|None = Field(default=None,max_length=200)
    @field_validator("reason")
    @classmethod
    def clean_reason(cls,value):
        if value is None: return None
        value=value.strip()
        if "<" in value or ">" in value: raise ValueError("Reason cannot contain HTML")
        return value or None
class RestoreBackup(BaseModel): confirmation: str = Field(min_length=1,max_length=100)
class PinBackup(BaseModel): pinned: bool
class JsonSave(BaseModel):
    content: str = Field(max_length=5242880)
    base_version: str = Field(min_length=64,max_length=64,pattern=r"^[0-9a-f]{64}$")
class ConfigSave(BaseModel):
    values: dict[str,object]
    base_version: str = Field(min_length=64,max_length=64,pattern=r"^[0-9a-f]{64}$")
class DatabaseFilter(BaseModel):
    column: str = Field(min_length=1,max_length=128)
    operator: Literal["equals","not_equals","contains","starts_with","greater_than","less_than","before","after","is_null","is_not_null"]
    value: object|None = None
class DatabaseUpdate(BaseModel):
    key: dict[str,object]
    values: dict[str,object]
    concurrency_token: str = Field(min_length=64,max_length=64,pattern=r"^[0-9a-f]{64}$")
class DatabaseCreate(BaseModel): values: dict[str,object]
class DatabaseDelete(BaseModel):
    key: dict[str,object]
    concurrency_token: str = Field(min_length=64,max_length=64,pattern=r"^[0-9a-f]{64}$")
    confirmation: str = Field(min_length=1,max_length=200)
class AgentHeartbeat(BaseModel):
    bot_id: str = Field(min_length=2,max_length=36,pattern=r"^[a-z0-9][a-z0-9_-]{1,35}$")
    instance_id: str = Field(min_length=6,max_length=41,pattern=r"^INST-[0-9a-fA-F-]{1,36}$")
    timestamp: datetime
    connected: bool
    ready: bool
    latency_ms: float|None = Field(default=None,ge=0,le=300000)
    guild_count: int|None = Field(default=None,ge=0,le=1000000)
    shard_count: int|None = Field(default=None,ge=1,le=10000)
    ready_shards: int|None = Field(default=None,ge=0,le=10000)
    maintenance_applied: bool|None = None
    @model_validator(mode="after")
    def consistent(self):
        if self.ready and not self.connected: raise ValueError("ready requires connected")
        if self.ready_shards is not None and self.shard_count is not None and self.ready_shards>self.shard_count: raise ValueError("ready_shards exceeds shard_count")
        return self
SNOWFLAKE=r"^[0-9]{1,32}$"
class DiscordRoleSnapshot(BaseModel):
    role_id:str=Field(pattern=SNOWFLAKE); name:str=Field(min_length=1,max_length=100); position:int=Field(ge=0,le=100000)
    colour:int=Field(default=0,ge=0,le=0xFFFFFF); managed:bool=False; permissions:list[str]=Field(default_factory=list,max_length=200); bot_has_role:bool=False
    @field_validator("permissions")
    @classmethod
    def permission_names(cls,v):
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",x) for x in v): raise ValueError("invalid permission name")
        return list(dict.fromkeys(v))
class DiscordChannelSnapshot(BaseModel):
    channel_id:str=Field(pattern=SNOWFLAKE); name:str=Field(min_length=1,max_length=100); type:Literal["text","voice","category","forum","announcement","stage","thread","public_thread","private_thread","news_thread"]
    category_id:str|None=Field(default=None,pattern=SNOWFLAKE); parent_id:str|None=Field(default=None,pattern=SNOWFLAKE); position:int=Field(default=0,ge=0,le=100000); permissions:list[str]=Field(default_factory=list,max_length=200)
    _permission_names=field_validator("permissions")(DiscordRoleSnapshot.permission_names.__func__)
class DiscordGuildData(BaseModel):
    guild_id:str=Field(pattern=SNOWFLAKE); name:str=Field(min_length=1,max_length=100); icon_url:str|None=Field(default=None,max_length=500)
    member_count:int|None=Field(default=None,ge=0,le=10000000); owner_id:str|None=Field(default=None,pattern=SNOWFLAKE)
    bot_member_id:str=Field(pattern=SNOWFLAKE); bot_nickname:str|None=Field(default=None,max_length=100); bot_role_ids:list[str]=Field(default_factory=list,max_length=500)
    guild_permissions:list[str]=Field(default_factory=list,max_length=200); roles:list[DiscordRoleSnapshot]=Field(default_factory=list); channels:list[DiscordChannelSnapshot]=Field(default_factory=list)
    truncated:bool=False
    _guild_permission_names=field_validator("guild_permissions")(DiscordRoleSnapshot.permission_names.__func__)
class DiscordSnapshotEnvelope(BaseModel):
    bot_id:str=Field(min_length=2,max_length=36,pattern=r"^[a-z0-9][a-z0-9_-]{1,35}$"); instance_id:str=Field(min_length=6,max_length=41,pattern=r"^INST-[0-9a-fA-F-]{1,36}$")
    snapshot_generated_at:datetime; guilds:list[DiscordGuildData]=Field(max_length=1000)
class BotMutation(BaseModel):
    id: str = Field(min_length=2, max_length=36)
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    folder: str = Field(min_length=1, max_length=500)
    entry_file: str = Field(min_length=1, max_length=255)
    python_executable: str = Field(min_length=1, max_length=500)
    accent_colour: str = "#5865f2"
    enabled: bool = True
    adapter: str = Field(default="python", pattern=r"^[a-z0-9_-]+$")
    data_root: str = Field(default=".",min_length=1,max_length=500)
    backup_include: str = Field(default="**/*",max_length=2000)
    backup_exclude: str = Field(default="",max_length=4000)
    restore_policy: Literal["requires_stop","supports_live"] = "requires_stop"
    @field_validator("id")
    @classmethod
    def safe_id(cls, value):
        if not BOT_ID.fullmatch(value): raise ValueError("Bot ID must contain only lowercase letters, numbers, hyphens, or underscores")
        return value
    @field_validator("accent_colour")
    @classmethod
    def colour(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value): raise ValueError("Accent colour must be a six-digit hex colour")
        return value.lower()
