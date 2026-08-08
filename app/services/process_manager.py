import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from app.adapters.base import BotHealth, BotState
from app.models import Bot

class ProcessConflict(RuntimeError): pass
class ProcessLaunchError(RuntimeError): pass

@dataclass
class ManagedProcess:
    process: asyncio.subprocess.Process
    started_at: datetime

class BotProcessManager:
    """In-process, lock-protected subprocess registry. Production should pair this with one app worker or an external supervisor."""
    def __init__(self):
        self._processes:dict[str,ManagedProcess]={}; self._states:dict[str,BotState]={}; self._locks:dict[str,asyncio.Lock]={}
    def _lock(self,bot_id): return self._locks.setdefault(bot_id,asyncio.Lock())
    async def get_status(self,bot_id:str,enabled:bool=True)->BotHealth:
        if not enabled: return BotHealth(BotState.DISABLED,detail="Registration disabled")
        managed=self._processes.get(bot_id)
        if not managed: return BotHealth(self._states.get(bot_id,BotState.OFFLINE))
        code=managed.process.returncode
        if code is not None:
            self._processes.pop(bot_id,None); state=BotState.OFFLINE if code==0 else BotState.CRASHED; self._states[bot_id]=state
            return BotHealth(state,detail=f"Process exited with code {code}")
        uptime=(datetime.now(timezone.utc)-managed.started_at).total_seconds()
        return BotHealth(self._states.get(bot_id,BotState.UNKNOWN),True,False,False,f"PID {managed.process.pid}; uptime {uptime:.0f}s; Discord state unknown")
    async def start_bot(self,bot:Bot)->BotHealth:
        async with self._lock(bot.id):
            current=await self.get_status(bot.id,bot.enabled)
            if not bot.enabled: raise ProcessConflict("Bot registration is disabled")
            if current.process_running or self._states.get(bot.id) in {BotState.STARTING,BotState.RESTARTING,BotState.STOPPING}: raise ProcessConflict("Bot process is already active or changing state")
            self._states[bot.id]=BotState.STARTING
            try:
                process=await asyncio.create_subprocess_exec(bot.python_executable,bot.entry_file,cwd=Path(bot.folder),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            except (OSError,ValueError) as exc:
                self._states[bot.id]=BotState.CRASHED; raise ProcessLaunchError("Bot process could not be launched") from exc
            self._processes[bot.id]=ManagedProcess(process,datetime.now(timezone.utc)); self._states[bot.id]=BotState.UNKNOWN
            return await self.get_status(bot.id)
    async def stop_bot(self,bot:Bot)->BotHealth:
        async with self._lock(bot.id):
            managed=self._processes.get(bot.id)
            if not managed or managed.process.returncode is not None: raise ProcessConflict("Bot process is not running")
            self._states[bot.id]=BotState.STOPPING; managed.process.terminate()
            try: await asyncio.wait_for(managed.process.wait(),10)
            except TimeoutError: managed.process.kill(); await managed.process.wait()
            self._processes.pop(bot.id,None); self._states[bot.id]=BotState.OFFLINE; return await self.get_status(bot.id)
    async def restart_bot(self,bot:Bot)->BotHealth:
        async with self._lock(bot.id):
            managed=self._processes.get(bot.id)
            if not managed or managed.process.returncode is not None: raise ProcessConflict("Bot process is not running")
            self._states[bot.id]=BotState.RESTARTING; managed.process.terminate(); await managed.process.wait(); self._processes.pop(bot.id,None)
            try: process=await asyncio.create_subprocess_exec(bot.python_executable,bot.entry_file,cwd=Path(bot.folder),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            except OSError as exc: self._states[bot.id]=BotState.CRASHED; raise ProcessLaunchError("Bot process could not be restarted") from exc
            self._processes[bot.id]=ManagedProcess(process,datetime.now(timezone.utc)); self._states[bot.id]=BotState.UNKNOWN; return await self.get_status(bot.id)

process_manager=BotProcessManager()
