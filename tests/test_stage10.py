from datetime import datetime, timedelta, timezone
import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from app.adapters.base import BotState
from app.agent import MaintenanceGate, MaintenancePolicyState, protect_interactive_item
from app.core.events import EventType
from app.models import ActivityEvent, AuditLog, Bot, BotMaintenance, User
from app.scheduler.service import SchedulerService
from app.scheduler.types import MaintenancePolicy, RegisteredTask, TaskResult
from app.services.bot_state import BotStateResolver, StateInputs
from app.services.maintenance import MaintenanceRequired, MaintenanceService


def objects(db):
    user=User(discord_id='100',username='owner',display_name='Owner')
    bot=Bot(id='events',display_name='Events',folder='.',entry_file='bot.py')
    db.add_all([user,bot]); db.commit(); return user,bot


def test_persistent_desired_applied_idempotency_and_planned_end(db):
    user,bot=objects(db); service=MaintenanceService(db)
    end=datetime.now(timezone.utc)-timedelta(minutes=1)
    row,op=service.set(bot,user,True,'Database maintenance','Scheduled maintenance.',end)
    assert op and row.enabled and row.applied_enabled is None
    assert service.payload(row,True)['sync_status']=='PENDING_SYNC'
    assert service.payload(row,True)['planned_end_passed'] is True
    assert service.require_maintenance(bot.id) is row
    row2,second=service.set(bot,user,True,'Database maintenance','Scheduled maintenance.',end)
    assert second is None
    assert db.scalar(select(ActivityEvent).where(ActivityEvent.event_type==EventType.BOT_MAINTENANCE_ENABLED.value))
    assert len(list(db.scalars(select(AuditLog).where(AuditLog.action==EventType.BOT_MAINTENANCE_ENABLED.value))))==1
    service.reconcile_applied(bot.id,'INST-one',True)
    assert service.payload(row,True)['sync_status']=='ACTIVE'
    disabled,_=service.set(bot,user,False); assert not disabled.enabled and disabled.applied_enabled is None
    _,repeat=service.set(bot,user,False); assert repeat is None


def test_gate_safe_user_guild_role_dm_and_interactive_recheck():
    gate=MaintenanceGate(); action=object(); safe=object()
    gate.register_safe(safe); gate.update({'enabled':True,'public_message':'Please wait','bypass_user_ids':['10'],'bypass_roles':[{'guild_id':'20','role_id':'30'}]})
    assert not gate.allowed(action=action,user_id='11') # DMs fail closed
    assert gate.allowed(action=safe,user_id='11')
    assert gate.allowed(action=action,user_id='10')
    assert gate.allowed(action=action,user_id='11',guild_id='20',role_ids=['30'])
    assert not gate.allowed(action=action,user_id='11',guild_id='21',role_ids=['30'])
    called=[]
    class Response:
        def is_done(self): return False
        async def send_message(self,*args,**kwargs): called.append((args,kwargs))
    interaction=SimpleNamespace(user=SimpleNamespace(id='11',roles=[]),guild=None,response=Response(),followup=None,command=None)
    item=SimpleNamespace(interaction_check=None)
    protect_interactive_item(item,gate)
    assert asyncio.run(item.interaction_check(interaction)) is False
    assert called[0][1]['ephemeral'] is True


def test_state_precedence_maintenance_does_not_hide_failures():
    resolver=BotStateResolver(30,30); now=datetime.now(timezone.utc)
    healthy=StateInputs(True,'running',True,True,started_at=now,connected=True,ready=True,last_heartbeat_at=now,maintenance=True)
    assert resolver.resolve(healthy,now) is BotState.MAINTENANCE
    crashed=StateInputs(True,'crashed',False,True,maintenance=True)
    assert resolver.resolve(crashed,now) is BotState.CRASHED
    disconnected=StateInputs(True,'running',True,True,started_at=now-timedelta(minutes=5),connected=False,ready=False,last_heartbeat_at=now,maintenance=True)
    assert resolver.resolve(disconnected,now) is BotState.DISCONNECTED


def test_scheduler_defaults_block_and_explicit_safe_runs(db):
    user,bot=objects(db); db.add(BotMaintenance(bot_id=bot.id,enabled=True,bypass_user_ids=[],bypass_roles=[])); db.commit()
    process=SimpleNamespace(status=lambda _: {'process_running':True,'discord_ready':True})
    scheduler=SchedulerService(lambda: db,process)
    async def handler(_): return TaskResult()
    blocked=RegisteredTask('weekly','Weekly','',handler)
    safe=RegisteredTask('cleanup','Cleanup','',handler,maintenance_policy=MaintenancePolicy.RUN_DURING_MAINTENANCE)
    maintenance=db.get(BotMaintenance,bot.id)
    assert scheduler._precondition(bot,blocked,maintenance)=='Skipped because the bot is in Maintenance Mode.'
    assert scheduler._precondition(bot,safe,maintenance) is None
