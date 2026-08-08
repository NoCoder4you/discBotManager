from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable
class BotState(str,Enum): ONLINE="online"; STARTING="starting"; RESTARTING="restarting"; DISCONNECTED="disconnected"; CRASHED="crashed"; CRASH_LOOP="crash_loop"; MAINTENANCE="maintenance"; DISABLED="disabled"; OFFLINE="offline"; STOPPING="stopping"; UNKNOWN="unknown"
@dataclass(frozen=True)
class BotHealth: state:BotState=BotState.UNKNOWN; process_running:bool=False; discord_connected:bool=False; discord_ready:bool=False; detail:str|None=None
class Danger(str,Enum): LOW="low"; MEDIUM="medium"; HIGH="high"; CRITICAL="critical"
@dataclass(frozen=True)
class QuickAction: key:str; name:str; description:str; required_permission:str; danger:Danger; confirmation_required:bool; handler:Callable[...,Awaitable[None]]
class BaseBotAdapter(ABC):
    async def get_health(self)->BotHealth: return BotHealth()
    async def get_status(self)->BotState: return (await self.get_health()).state
    def get_modules(self)->tuple[str,...]: return ()
    def get_commands(self)->tuple: return ()
    def get_cogs(self)->tuple: return ()
    def get_data_sources(self)->tuple: return ()
    def get_quick_actions(self)->tuple[QuickAction,...]: return ()
    def get_custom_permissions(self)->tuple[str,...]: return ()
