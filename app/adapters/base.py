from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable
class BotState(str,Enum): ONLINE="online"; RUNNING="running"; STARTING="starting"; RESTARTING="restarting"; DISCONNECTED="disconnected"; CRASHED="crashed"; CRASH_LOOP="crash_loop"; MAINTENANCE="maintenance"; DISABLED="disabled"; OFFLINE="offline"; STOPPING="stopping"; UNKNOWN="unknown"
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
    def get_quick_actions(self)->tuple[QuickAction,...]: return ()
    def get_custom_permissions(self)->tuple[str,...]: return ()
