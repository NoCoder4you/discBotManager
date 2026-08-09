import hashlib
from datetime import datetime, timedelta, timezone
import pytest
from app.adapters.base import BaseBotAdapter, DiscordCapability
from app.adapters.registry import register_adapter
from app.models import ActivityEvent, Bot, BotInstance, DiscordGuildSnapshot
from app.schemas import DiscordSnapshotEnvelope
from app.services.guild_snapshots import GuildSnapshotService, SnapshotRejected

class DiagnosticAdapter(BaseBotAdapter):
    def get_discord_capabilities(self):
        return (
            DiscordCapability("leaderboard","Weekly Leaderboard",required_channel_permissions=("view_channel","send_messages","embed_links"),channel_id="900000000000000003",allowed_channel_types=("text","announcement")),
            DiscordCapability("player_role","Player Role",required_guild_permissions=("manage_roles",),role_id="900000000000000005",requires_role_management=True),
        )
try: register_adapter("stage9-test",DiagnosticAdapter())
except ValueError: pass

def setup(db):
    secret="agent-secret"; bot=Bot(id="events",display_name="Events",folder=".",entry_file="bot.py",adapter="stage9-test",management_secret_hash=hashlib.sha256(secret.encode()).hexdigest()); db.add(bot)
    instance=BotInstance(bot_id=bot.id,instance_id="INST-000108",state="running",expected_running=True,python_executable="python",entry_file="bot.py",working_directory=".",discord_connected=True,discord_ready=True); db.add(instance); db.commit(); return bot,instance,secret

def envelope(embed=False,target_position=5,instance="INST-000108",generated=None):
    perms=["view_channel","send_messages"]+(["embed_links"] if embed else [])
    return DiscordSnapshotEnvelope.model_validate({"bot_id":"events","instance_id":instance,"snapshot_generated_at":generated or datetime.now(timezone.utc),"guilds":[{"guild_id":"900000000000000001","name":"Example Guild","member_count":2413,"bot_member_id":"900000000000000002","bot_role_ids":["900000000000000004"],"guild_permissions":["manage_roles"],"roles":[{"role_id":"900000000000000004","name":"Events Bot","position":4,"bot_has_role":True},{"role_id":"900000000000000005","name":"Events Team","position":target_position}],"channels":[{"channel_id":"900000000000000003","name":"weekly-leaderboard","type":"text","permissions":perms}]}]})

def test_snapshot_diagnostics_transition_and_string_ids(db):
    bot,instance,secret=setup(db); service=GuildSnapshotService(db)
    service.accept(envelope(),secret); view=service.view(service.rows(bot.id)[0])
    assert view["guild_id"]=="900000000000000001" and view["availability"]=="CURRENT"
    leaderboard=next(x for x in view["diagnostics"] if x["capability_id"]=="leaderboard"); role=next(x for x in view["diagnostics"] if x["capability_id"]=="player_role")
    assert leaderboard["status"]=="FAIL" and leaderboard["missing_permissions"]==["embed_links"]
    assert role["status"]=="FAIL"
    service.accept(envelope(embed=True,target_position=3),secret); service.accept(envelope(embed=True,target_position=3),secret)
    events=list(db.query(ActivityEvent).filter(ActivityEvent.event_type=="DISCORD_CAPABILITY_RECOVERED"))
    assert len(events)==2  # one recovery for each registered capability, never repeated

def test_stale_instance_and_bad_credential_do_not_replace(db):
    bot,_,secret=setup(db); service=GuildSnapshotService(db); service.accept(envelope(),secret)
    with pytest.raises(SnapshotRejected): service.accept(envelope(embed=True,instance="INST-000107"),secret)
    with pytest.raises(SnapshotRejected): service.accept(envelope(embed=True),"wrong")
    assert next(x for x in service.rows(bot.id)[0].diagnostics if x["capability_id"]=="leaderboard")["status"]=="FAIL"

def test_cached_and_stale_are_not_false_passes(db):
    bot,instance,secret=setup(db); service=GuildSnapshotService(db); service.accept(envelope(embed=True,target_position=3),secret); row=service.rows(bot.id)[0]
    instance.discord_ready=False; db.commit(); assert service.view(row)["availability"]=="CACHED"
    row.received_at=datetime.now(timezone.utc)-timedelta(seconds=301); db.commit(); view=service.view(row)
    assert view["availability"]=="STALE" and all(x["status"]=="UNKNOWN" for x in view["diagnostics"])
