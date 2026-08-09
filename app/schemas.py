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
    @model_validator(mode="after")
    def consistent(self):
        if self.ready and not self.connected: raise ValueError("ready requires connected")
        if self.ready_shards is not None and self.shard_count is not None and self.ready_shards>self.shard_count: raise ValueError("ready_shards exceeds shard_count")
        return self
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
