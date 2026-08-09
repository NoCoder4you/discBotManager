import hashlib, hmac, json
from datetime import datetime, timezone
from sqlalchemy import delete, select
from app.adapters.registry import get_adapter
from app.core.config import get_settings
from app.models import ActivityEvent, Bot, BotInstance, DiscordDiagnosticState, DiscordGuildSnapshot, utcnow

class SnapshotRejected(RuntimeError):
    def __init__(self,message,status_code=409): super().__init__(message); self.status_code=status_code
def aware(v): return v if v is None or v.tzinfo else v.replace(tzinfo=timezone.utc)
def friendly(name): return name.replace("_"," ").title()

class GuildSnapshotService:
    """Authenticated, bot-scoped cache and central capability diagnostic engine."""
    def __init__(self,db,settings=None): self.db=db; self.settings=settings or get_settings()
    def authenticate_instance(self,envelope,credential):
        bot=self.db.get(Bot,envelope.bot_id); supplied=hashlib.sha256((credential or "").encode()).hexdigest()
        if not bot or not credential or not bot.management_secret_hash or not hmac.compare_digest(bot.management_secret_hash,supplied): raise SnapshotRejected("Invalid agent credential",401)
        instance=self.db.scalar(select(BotInstance).where(BotInstance.bot_id==bot.id).order_by(BotInstance.id.desc()).limit(1))
        if not instance or instance.instance_id!=envelope.instance_id or instance.ended_at is not None: raise SnapshotRejected("Snapshot does not belong to the current instance")
        if not bot.enabled or instance.state not in {"running","starting","restarting"}: raise SnapshotRejected("Current bot instance is unavailable")
        now=datetime.now(timezone.utc)
        if abs((now-aware(envelope.snapshot_generated_at)).total_seconds())>self.settings.discord_snapshot_stale_seconds*2: raise SnapshotRejected("Snapshot timestamp is outside the accepted window")
        return bot,instance
    def accept(self,envelope,credential):
        bot,instance=self.authenticate_instance(envelope,credential); now=utcnow(); incoming={g.guild_id:g for g in envelope.guilds}
        previous={x.guild_id:x for x in self.db.scalars(select(DiscordGuildSnapshot).where(DiscordGuildSnapshot.bot_id==bot.id))}
        for guild_id,guild in incoming.items():
            data=guild.model_dump(mode="json"); diagnostics=self.diagnose(bot,data,envelope.snapshot_generated_at)
            row=previous.get(guild_id) or DiscordGuildSnapshot(bot_id=bot.id,guild_id=guild_id)
            if not row.id: self.db.add(row)
            row.instance_id=instance.instance_id; row.generated_at=envelope.snapshot_generated_at; row.received_at=now; row.payload=data; row.diagnostics=diagnostics
            self._transitions(bot.id,guild_id,diagnostics)
            if guild_id not in previous: self.db.add(ActivityEvent(event_type="BOT_GUILD_JOINED",bot_id=bot.id,payload={"guild_id":guild_id,"guild_name":guild.name,"instance_id":instance.instance_id}))
        removed=set(previous)-set(incoming)
        for guild_id in removed:
            self.db.add(ActivityEvent(event_type="BOT_GUILD_REMOVED",bot_id=bot.id,payload={"guild_id":guild_id,"guild_name":previous[guild_id].payload.get("name"),"instance_id":instance.instance_id}))
            self.db.delete(previous[guild_id]); self.db.execute(delete(DiscordDiagnosticState).where(DiscordDiagnosticState.bot_id==bot.id,DiscordDiagnosticState.guild_id==guild_id))
        self.db.commit(); return {"accepted":True,"guild_count":len(incoming),"received_at":now.isoformat()}
    def diagnose(self,bot,data,checked_at):
        roles={x["role_id"]:x for x in data["roles"]}; channels={x["channel_id"]:x for x in data["channels"]}; guild_perms=set(data["guild_permissions"]); admin="administrator" in guild_perms
        held=[r for r in roles.values() if r["bot_has_role"] and not r["managed"]]; highest=max(held,key=lambda r:(r["position"],int(r["role_id"])),default=None)
        results=[]
        for cap in get_adapter(bot.adapter).get_discord_capabilities():
            missing=[] if admin else [p for p in cap.required_guild_permissions if p not in guild_perms]; target_type="guild"; target_id=None; reasons=[]
            channel_id=cap.channel_id
            if channel_id:
                target_type="channel"; target_id=channel_id; channel=channels.get(channel_id)
                if not channel: reasons.append("Target channel not found")
                else:
                    if cap.allowed_channel_types and channel["type"] not in cap.allowed_channel_types: reasons.append(f"Wrong channel type: {channel['type']}")
                    if not admin: missing += [p for p in cap.required_channel_permissions if p not in channel["permissions"]]
            role_id=cap.role_id
            if role_id:
                target_type="role"; target_id=role_id; role=roles.get(role_id)
                if not role: reasons.append("Target role not found")
                elif cap.requires_role_management:
                    if role["managed"]: reasons.append("Target role is managed by Discord")
                    if not admin and "manage_roles" not in guild_perms: missing.append("manage_roles")
                    if not highest or highest["position"]<=role["position"]: reasons.append("Target role is equal to or above the bot's highest role")
            missing=list(dict.fromkeys(missing)); failed=bool(missing or reasons); status="FAIL" if failed else "PASS"
            fingerprint=hashlib.sha256(f"{bot.id}|{data['guild_id']}|{cap.id}|{target_type}|{target_id or ''}".encode()).hexdigest()
            results.append({"diagnostic_id":fingerprint[:16],"fingerprint":fingerprint,"capability_id":cap.id,"capability_name":cap.name,"guild_id":data["guild_id"],"target_type":target_type,"target_id":target_id,"severity":cap.severity,"status":status,"title":f"{cap.name} {'is not ready' if failed else 'is ready'}","description":"; ".join(reasons) or (f"Missing: {', '.join(map(friendly,missing))}" if missing else cap.description or "All declared requirements are available."),"required_permissions":list(cap.required_guild_permissions+cap.required_channel_permissions),"missing_permissions":missing,"checked_at":checked_at.isoformat()})
        if admin: results.append({"diagnostic_id":"administrator","fingerprint":hashlib.sha256(f"{bot.id}|{data['guild_id']}|administrator".encode()).hexdigest(),"capability_id":"administrator_awareness","capability_name":"Administrator awareness","guild_id":data["guild_id"],"target_type":"guild","target_id":None,"severity":"INFO","status":"WARNING","title":"Broad permission granted","description":"Administrator grants effective permissions, but Discord role hierarchy restrictions still apply.","required_permissions":[],"missing_permissions":[],"checked_at":checked_at.isoformat()})
        return results
    def _transitions(self,bot_id,guild_id,diagnostics):
        for d in diagnostics:
            if d["status"] not in {"PASS","FAIL"}: continue
            state=self.db.scalar(select(DiscordDiagnosticState).where(DiscordDiagnosticState.bot_id==bot_id,DiscordDiagnosticState.fingerprint==d["fingerprint"])); old=state.status if state else None
            if state is None: state=DiscordDiagnosticState(bot_id=bot_id,guild_id=guild_id,fingerprint=d["fingerprint"],status=d["status"]); self.db.add(state)
            else: state.status=d["status"]; state.updated_at=utcnow()
            if old and old!=d["status"]: self.db.add(ActivityEvent(event_type="DISCORD_CAPABILITY_FAILED" if d["status"]=="FAIL" else "DISCORD_CAPABILITY_RECOVERED",bot_id=bot_id,payload={"guild_id":guild_id,"capability_id":d["capability_id"],"target_id":d["target_id"],"severity":d["severity"],"diagnostic_id":d["diagnostic_id"]}))
    def availability(self,row,instance=None,now=None):
        now=now or datetime.now(timezone.utc); age=(now-aware(row.received_at)).total_seconds()
        if age>self.settings.discord_snapshot_stale_seconds: return "STALE"
        if not instance or instance.instance_id!=row.instance_id or not instance.discord_ready: return "CACHED"
        return "CURRENT"
    def rows(self,bot_id): return list(self.db.scalars(select(DiscordGuildSnapshot).where(DiscordGuildSnapshot.bot_id==bot_id).order_by(DiscordGuildSnapshot.guild_id)))
    def current_instance(self,bot_id): return self.db.scalar(select(BotInstance).where(BotInstance.bot_id==bot_id).order_by(BotInstance.id.desc()).limit(1))
    def view(self,row):
        state=self.availability(row,self.current_instance(row.bot_id)); diagnostics=row.diagnostics
        if state=="STALE": diagnostics=[{**d,"status":"UNKNOWN","description":"Snapshot is stale; this check cannot be confirmed."} for d in diagnostics]
        held=[r for r in row.payload["roles"] if r["bot_has_role"] and not r["managed"]]; highest=max(held,key=lambda r:(r["position"],int(r["role_id"])),default=None)
        return {**row.payload,"bot_highest_role":highest,"instance_id":row.instance_id,"snapshot_generated_at":aware(row.generated_at).isoformat(),"snapshot_received_at":aware(row.received_at).isoformat(),"availability":state,"diagnostics":diagnostics,"permission_health":{"passed":sum(d["status"]=="PASS" for d in diagnostics),"warnings":sum(d["status"]=="WARNING" for d in diagnostics),"failures":sum(d["status"]=="FAIL" for d in diagnostics),"unknown":sum(d["status"]=="UNKNOWN" for d in diagnostics)}}
