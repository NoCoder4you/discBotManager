"""Dependency-light management agent for managed Discord bots.

This module deliberately has no FastAPI imports and reporting failures never escape
the background task or Discord lifecycle callbacks.
"""
from __future__ import annotations
import asyncio, json, logging, os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

log=logging.getLogger(__name__)


class BotManagementAgent:
    def __init__(self,bot_id:str,instance_id:str,credential:str,endpoint:str="http://127.0.0.1:8765/internal/agent/heartbeat",interval:float=10):
        self.bot_id=bot_id; self.instance_id=instance_id; self._credential=credential; self.endpoint=endpoint; self.interval=interval
        self.connected=False; self.ready=False; self.latency_ms=None; self.guild_count=None; self._task=None; self._stop=asyncio.Event(); self._available=None

    @classmethod
    def from_environment(cls):
        required=("BOT_MANAGEMENT_BOT_ID","BOT_INSTANCE_ID","BOT_MANAGEMENT_SECRET")
        if not all(os.getenv(x) for x in required): raise ValueError("Management agent environment is incomplete")
        return cls(os.environ[required[0]],os.environ[required[1]],os.environ[required[2]],os.getenv("BOT_MANAGEMENT_HEARTBEAT_URL","http://127.0.0.1:8765/internal/agent/heartbeat"),float(os.getenv("BOT_HEARTBEAT_INTERVAL_SECONDS","10")))

    def update(self,*,connected=None,ready=None,latency_ms=None,guild_count=None):
        if connected is not None:
            self.connected=connected
            if not connected: self.ready=False
        if ready is not None: self.ready=bool(ready and self.connected)
        self.latency_ms=latency_ms; self.guild_count=guild_count

    async def start(self):
        if not self._task or self._task.done(): self._stop.clear(); self._task=asyncio.create_task(self._run(),name="bot-management-heartbeat")

    async def stop(self):
        self._stop.set()
        if self._task: await self._task

    async def send_once(self):
        payload=json.dumps({"bot_id":self.bot_id,"instance_id":self.instance_id,"timestamp":datetime.now(timezone.utc).isoformat(),"connected":self.connected,"ready":self.ready,"latency_ms":self.latency_ms,"guild_count":self.guild_count},separators=(",",":")).encode()
        request=Request(self.endpoint,data=payload,headers={"Content-Type":"application/json","X-Bot-Management-Secret":self._credential},method="POST")
        await asyncio.to_thread(lambda:urlopen(request,timeout=3).read())

    async def _run(self):
        delay=1.0
        while not self._stop.is_set():
            try:
                await self.send_once()
                if self._available is not True: log.info("Management heartbeat %s","restored" if self._available is False else "connection established")
                self._available=True; delay=self.interval
            except Exception:
                if self._available is not False: log.warning("Management heartbeat unavailable")
                self._available=False; delay=min(30,max(1,delay*2))
            try: await asyncio.wait_for(self._stop.wait(),delay)
            except asyncio.TimeoutError: pass


def integrate_discord_client(client, agent: BotManagementAgent):
    """Register reconnect-safe discord.py listeners without subclassing Client."""
    async def connected(): agent.update(connected=True,ready=False)
    async def disconnected(): agent.update(connected=False,ready=False)
    async def ready():
        latency=max(0,client.latency*1000) if client.latency is not None else None
        agent.update(connected=True,ready=True,latency_ms=latency,guild_count=len(client.guilds))
    client.add_listener(connected,"on_connect"); client.add_listener(disconnected,"on_disconnect"); client.add_listener(ready,"on_ready")
    return agent
