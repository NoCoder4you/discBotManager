from __future__ import annotations
from typing import Protocol
import httpx
from app.adapters.base import BotHealth, BotState
from app.core.config import get_settings

class ProcessConflict(RuntimeError): pass
class ProcessLaunchError(RuntimeError): pass
class SupervisorUnavailable(RuntimeError): pass

class SupervisorBackend(Protocol):
    async def status(self,bot_id:str)->dict: ...
    async def action(self,bot_id:str,action:str)->dict: ...
    async def health(self)->dict: ...
    async def reconcile(self)->list[dict]: ...
    async def console(self,bot_id:str,after:int=0)->dict: ...
    async def telemetry(self,bot_id:str)->dict: ...
    async def telemetry_history(self,bot_id:str,minutes:int)->dict: ...

class SupervisorClient:
    def __init__(self,url=None,secret=None,timeout=None):
        settings=get_settings(); self.url=(url or settings.supervisor_url).rstrip("/"); self.secret=secret if secret is not None else settings.supervisor_secret; self.timeout=timeout or settings.supervisor_timeout
    async def _request(self,method,path):
        try:
            async with httpx.AsyncClient(base_url=self.url,timeout=self.timeout) as client: response=await client.request(method,path,headers={"X-Supervisor-Secret":self.secret})
        except httpx.RequestError as exc: raise SupervisorUnavailable("Unable to contact the process supervisor.") from exc
        if response.status_code==409: raise ProcessConflict(response.json().get("detail","Process operation conflict"))
        if response.status_code>=400: raise SupervisorUnavailable("The process supervisor rejected the request.")
        return response.json()
    async def status(self,bot_id): return await self._request("GET",f"/internal/bots/{bot_id}")
    async def action(self,bot_id,action): return await self._request("POST",f"/internal/bots/{bot_id}/{action}")
    async def health(self): return await self._request("GET","/internal/health")
    async def reconcile(self): return await self._request("POST","/internal/reconcile")
    async def console(self,bot_id,after=0): return await self._request("GET",f"/internal/bots/{bot_id}/console?after={after}")
    async def telemetry(self,bot_id): return await self._request("GET",f"/internal/bots/{bot_id}/telemetry")
    async def telemetry_history(self,bot_id,minutes): return await self._request("GET",f"/internal/bots/{bot_id}/telemetry/history?minutes={minutes}")

class BotProcessManager:
    """Stable application boundary for the independently running supervisor."""
    def __init__(self,client:SupervisorBackend|None=None): self.client=client or SupervisorClient()
    @staticmethod
    def _health(payload):
        return BotHealth(state=BotState(payload.get("state","unknown")),process_running=payload.get("process_running",False),discord_connected=payload.get("discord_connected",False),discord_ready=payload.get("discord_ready",False),pid=payload.get("pid"),instance_id=payload.get("instance_id"),uptime_seconds=payload.get("uptime_seconds"),supervisor_available=True,latency_ms=payload.get("latency_ms"),guild_count=payload.get("guild_count"),last_heartbeat_at=payload.get("last_heartbeat_at"),ready_at=payload.get("ready_at"),last_ready_at=payload.get("last_ready_at"),heartbeat_fresh=payload.get("heartbeat_fresh",False))
    async def get_status(self,bot_id,enabled=True):
        if not enabled: return BotHealth(BotState.DISABLED,detail="Registration disabled")
        try: return self._health(await self.client.status(bot_id))
        except SupervisorUnavailable: return BotHealth(BotState.UNKNOWN,detail="Supervisor unavailable; bot state cannot currently be confirmed",supervisor_available=False)
    async def _action(self,bot,action):
        if not bot.enabled: raise ProcessConflict("Bot registration is disabled")
        try: return self._health(await self.client.action(bot.id,action))
        except SupervisorUnavailable as exc: raise ProcessLaunchError(f"Unable to {action} bot because the supervisor is unavailable.") from exc
    async def start_bot(self,bot): return await self._action(bot,"start")
    async def stop_bot(self,bot): return await self._action(bot,"stop")
    async def restart_bot(self,bot): return await self._action(bot,"restart")
    async def supervisor_health(self):
        try: return {**await self.client.health(),"available":True}
        except SupervisorUnavailable: return {"status":"unavailable","available":False,"managed_processes":None}
    async def reconcile(self):
        try: return await self.client.reconcile()
        except SupervisorUnavailable: return []

process_manager=BotProcessManager()
