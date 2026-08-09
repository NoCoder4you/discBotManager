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
        self.connected=False; self.ready=False; self.latency_ms=None; self.guild_count=None; self._task=None; self._snapshot_task=None; self._client=None; self._snapshot_requested=asyncio.Event(); self._stop=asyncio.Event(); self._available=None

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
        if self._client and (not self._snapshot_task or self._snapshot_task.done()): self._snapshot_task=asyncio.create_task(self._snapshot_loop(),name="bot-management-guild-snapshots")

    async def stop(self):
        self._stop.set(); self._snapshot_requested.set()
        if self._task: await self._task
        if self._snapshot_task: await self._snapshot_task

    def request_guild_snapshot(self): self._snapshot_requested.set()

    def build_guild_snapshot(self):
        def names(perms): return [name for name,value in perms if value]
        guilds=[]
        for guild in self._client.guilds:
            member=guild.me
            roles=[{"role_id":str(r.id),"name":r.name,"position":r.position,"colour":r.colour.value,"managed":r.managed,"permissions":names(r.permissions),"bot_has_role":r in member.roles} for r in guild.roles]
            channels=[]; aliases={"news":"announcement"}
            for c in guild.channels:
                kind=aliases.get(str(c.type),str(c.type))
                if kind not in {"text","voice","category","forum","announcement","stage","thread","public_thread","private_thread","news_thread"}: continue
                channels.append({"channel_id":str(c.id),"name":c.name,"type":kind,"category_id":str(c.category_id) if getattr(c,"category_id",None) else None,"parent_id":str(c.parent_id) if getattr(c,"parent_id",None) else None,"position":c.position,"permissions":names(c.permissions_for(member))})
            guilds.append({"guild_id":str(guild.id),"name":guild.name,"icon_url":str(guild.icon.url) if guild.icon else None,"member_count":guild.member_count,"owner_id":str(guild.owner_id) if guild.owner_id else None,"bot_member_id":str(member.id),"bot_nickname":member.nick,"bot_role_ids":[str(r.id) for r in member.roles],"guild_permissions":names(member.guild_permissions),"roles":roles,"channels":channels,"truncated":False})
        return {"bot_id":self.bot_id,"instance_id":self.instance_id,"snapshot_generated_at":datetime.now(timezone.utc).isoformat(),"guilds":guilds}

    async def send_guild_snapshot(self):
        endpoint=os.getenv("BOT_MANAGEMENT_GUILD_SNAPSHOT_URL",self.endpoint.rsplit("/heartbeat",1)[0]+"/guild-snapshot")
        payload=json.dumps(self.build_guild_snapshot(),separators=(",",":")).encode(); request=Request(endpoint,data=payload,headers={"Content-Type":"application/json","X-Bot-Management-Secret":self._credential},method="POST")
        await asyncio.to_thread(lambda:urlopen(request,timeout=5).read())

    async def _snapshot_loop(self):
        interval=float(os.getenv("DISCORD_SNAPSHOT_REFRESH_SECONDS","300"))
        while not self._stop.is_set():
            if self.ready:
                try: await self.send_guild_snapshot()
                except Exception: log.exception("Management guild snapshot unavailable")
            self._snapshot_requested.clear()
            try: await asyncio.wait_for(self._snapshot_requested.wait(),interval)
            except asyncio.TimeoutError: pass

    async def send_once(self):
        payload=json.dumps({"bot_id":self.bot_id,"instance_id":self.instance_id,"timestamp":datetime.now(timezone.utc).isoformat(),"connected":self.connected,"ready":self.ready,"latency_ms":self.latency_ms,"guild_count":self.guild_count},separators=(",",":")).encode()
        request=Request(self.endpoint,data=payload,headers={"Content-Type":"application/json","X-Bot-Management-Secret":self._credential},method="POST")
        raw=await asyncio.to_thread(lambda:urlopen(request,timeout=3).read())
        if json.loads(raw or b"{}").get("guild_snapshot_requested"): self.request_guild_snapshot()

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
    agent._client=client
    async def connected(): agent.update(connected=True,ready=False)
    async def disconnected(): agent.update(connected=False,ready=False)
    async def ready():
        latency=max(0,client.latency*1000) if client.latency is not None else None
        agent.update(connected=True,ready=True,latency_ms=latency,guild_count=len(client.guilds)); agent.request_guild_snapshot()
    client.add_listener(connected,"on_connect"); client.add_listener(disconnected,"on_disconnect"); client.add_listener(ready,"on_ready")
    async def changed(*_): agent.request_guild_snapshot()
    for event in ("on_guild_join","on_guild_remove","on_guild_update","on_guild_channel_create","on_guild_channel_delete","on_guild_channel_update","on_guild_role_create","on_guild_role_delete","on_guild_role_update"): client.add_listener(changed,event)
    async def member_changed(_,after):
        me=getattr(getattr(after,"guild",None),"me",None)
        if me and getattr(after,"id",None)==me.id: agent.request_guild_snapshot()
    client.add_listener(member_changed,"on_member_update")
    return agent
