"""Dependency-light management agent for managed Discord bots.

This module deliberately has no FastAPI imports and reporting failures never escape
the background task or Discord lifecycle callbacks.
"""
from __future__ import annotations
import asyncio, json, logging, os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.request import Request, urlopen

log=logging.getLogger(__name__)
DEFAULT_MAINTENANCE_MESSAGE="This bot is currently unavailable while maintenance is being carried out. Please try again later."

@dataclass(frozen=True)
class MaintenancePolicyState:
    enabled:bool=False; reason:str|None=None; public_message:str|None=None
    bypass_user_ids:frozenset[str]=frozenset(); bypass_roles:tuple[tuple[str,str],...]=()

class MaintenanceGate:
    """Lock-free immutable policy reads for Discord's command hot path."""
    def __init__(self,state=None): self._state=state or MaintenancePolicyState(); self._safe:set[int]=set()
    @property
    def state(self): return self._state
    def update(self,payload):
        roles=tuple((str(x.get("guild_id")),str(x.get("role_id"))) for x in payload.get("bypass_roles",()) if isinstance(x,dict) and x.get("guild_id") and x.get("role_id"))
        self._state=MaintenancePolicyState(bool(payload.get("enabled")),payload.get("reason"),payload.get("public_message"),frozenset(map(str,payload.get("bypass_user_ids",()))),roles)
    def register_safe(self,action): self._safe.add(id(action)); return action
    def allowed(self,*,action=None,user_id=None,guild_id=None,role_ids=()):
        state=self._state
        if not state.enabled:return True
        if id(action) in self._safe or bool(getattr(action,"maintenance_allowed",False)) or bool(getattr(action,"extras",{}).get("maintenance_allowed")):return True
        if user_id is not None and str(user_id) in state.bypass_user_ids:return True
        roles={str(x) for x in role_ids}; guild=str(guild_id) if guild_id is not None else None
        return any(g==guild and r in roles for g,r in state.bypass_roles)
    def message(self): return self._state.public_message or DEFAULT_MAINTENANCE_MESSAGE
    def check_context(self,context,action=None):
        author=getattr(context,"author",None); guild=getattr(context,"guild",None)
        return self.allowed(action=action or getattr(context,"command",None),user_id=getattr(author,"id",None),guild_id=getattr(guild,"id",None),role_ids=(getattr(x,"id",x) for x in getattr(author,"roles",())))
    def check_interaction(self,interaction,action=None):
        user=getattr(interaction,"user",None); guild=getattr(interaction,"guild",None)
        return self.allowed(action=action or getattr(interaction,"command",None),user_id=getattr(user,"id",None),guild_id=getattr(guild,"id",None),role_ids=(getattr(x,"id",x) for x in getattr(user,"roles",())))
    async def respond_interaction(self,interaction):
        response=getattr(interaction,"response",None)
        if response and not response.is_done(): await response.send_message(self.message(),ephemeral=True)
        elif getattr(interaction,"followup",None): await interaction.followup.send(self.message(),ephemeral=True)


class BotManagementAgent:
    def __init__(self,bot_id:str,instance_id:str,credential:str,endpoint:str="http://127.0.0.1:8765/internal/agent/heartbeat",interval:float=10):
        self.bot_id=bot_id; self.instance_id=instance_id; self._credential=credential; self.endpoint=endpoint; self.interval=interval
        self.connected=False; self.ready=False; self.latency_ms=None; self.guild_count=None; self.maintenance=MaintenanceGate(); self._task=None; self._snapshot_task=None; self._client=None; self._snapshot_requested=asyncio.Event(); self._stop=asyncio.Event(); self._available=None

    @classmethod
    def from_environment(cls):
        required=("BOT_MANAGEMENT_BOT_ID","BOT_INSTANCE_ID","BOT_MANAGEMENT_SECRET")
        if not all(os.getenv(x) for x in required): raise ValueError("Management agent environment is incomplete")
        agent=cls(os.environ[required[0]],os.environ[required[1]],os.environ[required[2]],os.getenv("BOT_MANAGEMENT_HEARTBEAT_URL","http://127.0.0.1:8765/internal/agent/heartbeat"),float(os.getenv("BOT_HEARTBEAT_INTERVAL_SECONDS","10")))
        # Supervisor injects this before the Discord client starts: no Ready/open race.
        try: agent.maintenance.update(json.loads(os.getenv("BOT_MAINTENANCE_STATE","{}")))
        except (TypeError,ValueError): agent.maintenance.update({"enabled":True,"public_message":DEFAULT_MAINTENANCE_MESSAGE})
        return agent

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
        payload=json.dumps({"bot_id":self.bot_id,"instance_id":self.instance_id,"timestamp":datetime.now(timezone.utc).isoformat(),"connected":self.connected,"ready":self.ready,"latency_ms":self.latency_ms,"guild_count":self.guild_count,"maintenance_applied":self.maintenance.state.enabled},separators=(",",":")).encode()
        request=Request(self.endpoint,data=payload,headers={"Content-Type":"application/json","X-Bot-Management-Secret":self._credential},method="POST")
        raw=await asyncio.to_thread(lambda:urlopen(request,timeout=3).read())
        response=json.loads(raw or b"{}")
        if response.get("guild_snapshot_requested"): self.request_guild_snapshot()
        if isinstance(response.get("maintenance"),dict): self.maintenance.update(response["maintenance"])

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
    # Prefix commands use discord.py's global check before invocation.
    if hasattr(client,"add_check"):
        async def maintenance_prefix_check(context):
            allowed=agent.maintenance.check_context(context)
            if not allowed: await context.send(agent.maintenance.message())
            return allowed
        client.add_check(maintenance_prefix_check)
    # Slash and context-menu commands share CommandTree.interaction_check.
    tree=getattr(client,"tree",None)
    if tree:
        previous=getattr(tree,"interaction_check",None)
        async def maintenance_interaction_check(interaction):
            if not agent.maintenance.check_interaction(interaction): await agent.maintenance.respond_interaction(interaction); return False
            return await previous(interaction) if previous else True
        tree.interaction_check=maintenance_interaction_check
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

def maintenance_safe(action):
    """Trusted-code marker for commands, callbacks, views, or modals."""
    setattr(action,"maintenance_allowed",True); return action

def protect_interactive_item(item,gate:MaintenanceGate,maintenance_allowed=False):
    """Apply the same policy to a View/Modal at submission time (including persistent views)."""
    previous=getattr(item,"interaction_check",None)
    if maintenance_allowed: gate.register_safe(item)
    async def check(interaction):
        if not gate.check_interaction(interaction,item): await gate.respond_interaction(interaction); return False
        return await previous(interaction) if previous else True
    item.interaction_check=check
    return item
