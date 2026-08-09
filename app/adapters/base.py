from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable
from app.scheduler.types import RegisteredTask
class BotState(str,Enum): ONLINE="online"; RUNNING="running"; STARTING="starting"; RESTARTING="restarting"; DISCONNECTED="disconnected"; CRASHED="crashed"; CRASH_LOOP="crash_loop"; MAINTENANCE="maintenance"; DISABLED="disabled"; OFFLINE="offline"; STOPPING="stopping"; UNKNOWN="unknown"
class DatabaseEditPolicy(str,Enum): LIVE_EDIT_SUPPORTED="live_edit_supported"; EDIT_REQUIRES_BOT_STOP="edit_requires_bot_stop"
@dataclass(frozen=True)
class BotHealth:
    state:BotState=BotState.UNKNOWN; process_running:bool=False; discord_connected:bool=False; discord_ready:bool=False; detail:str|None=None
    pid:int|None=None; instance_id:str|None=None; uptime_seconds:float|None=None; supervisor_available:bool=True
    latency_ms:float|None=None; guild_count:int|None=None; last_heartbeat_at:str|None=None; ready_at:str|None=None; last_ready_at:str|None=None; heartbeat_fresh:bool=False
class Danger(str,Enum): LOW="low"; MEDIUM="medium"; HIGH="high"; CRITICAL="critical"
@dataclass(frozen=True)
class QuickAction: key:str; name:str; description:str; required_permission:str; danger:Danger; confirmation_required:bool; handler:Callable[...,Awaitable[None]]
@dataclass(frozen=True)
class ConfigField:
    key:str; label:str; type:str="string"; description:str=""; default:Any=None; required:bool=False
    editable:bool=True; sensitive:bool=False; choices:tuple[str,...]=(); minimum:float|None=None
    maximum:float|None=None; step:float|None=None; requires_restart:bool=False
@dataclass(frozen=True)
class DataSource:
    id:str; name:str; path:str; description:str=""; type:str="json"; editable:bool=False
    validator:Callable[[Any],Any]|type|None=None; sensitive_fields:tuple[str,...]=()
    danger:Danger=Danger.LOW; config_fields:tuple[ConfigField,...]=()
@dataclass(frozen=True)
class DatabaseColumn:
    key:str; label:str=""; type:str|None=None; editable:bool=False; hidden:bool=False
    sensitive:bool=False; nullable:bool|None=None; choices:tuple[str,...]=(); minimum:float|None=None
    maximum:float|None=None; validator:Callable[[Any],Any]|None=None
@dataclass(frozen=True)
class DatabaseTable:
    name:str; label:str=""; visible:bool=True; editable:bool=False; allow_insert:bool=False
    allow_delete:bool=False; columns:tuple[DatabaseColumn,...]=(); search_columns:tuple[str,...]=()
@dataclass(frozen=True)
class DatabaseSource:
    id:str; label:str; path:str; editable:bool=False; tables:tuple[DatabaseTable,...]=()
    live_edit_supported:bool=True
    edit_policy:DatabaseEditPolicy|None=None
    validator:Callable[[str],Any]|None=None
    @property
    def mutation_policy(self)->DatabaseEditPolicy:
        return self.edit_policy or (DatabaseEditPolicy.LIVE_EDIT_SUPPORTED if self.live_edit_supported else DatabaseEditPolicy.EDIT_REQUIRES_BOT_STOP)
class BaseBotAdapter(ABC):
    supports_heartbeat: bool=False
    supports_discord_status: bool=False
    async def get_health(self)->BotHealth: return BotHealth()
    async def get_status(self)->BotState: return (await self.get_health()).state
    def get_modules(self)->tuple[str,...]: return ()
    def get_commands(self)->tuple: return ()
    def get_cogs(self)->tuple: return ()
    def get_data_sources(self)->tuple: return ()
    def get_config_schema(self)->tuple[DataSource,...]: return ()
    def get_database_sources(self)->tuple[DatabaseSource,...]: return ()
    def get_quick_actions(self)->tuple[QuickAction,...]: return ()
    def get_tasks(self)->tuple[RegisteredTask,...]: return ()
    def get_custom_permissions(self)->tuple[str,...]: return ()
