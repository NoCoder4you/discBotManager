import asyncio
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

import pytest
import hashlib
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent import BotManagementAgent
from app.database import Base
from app.models import ActivityEvent, Bot, BotInstance
from app.schemas import AgentHeartbeat
from app.services.bot_state import BotStateResolver, StateInputs
from app.services.heartbeat import HeartbeatRejected, HeartbeatService

NOW=datetime.now(timezone.utc)

def inputs(**overrides):
    values=dict(enabled=True,process_state='running',process_running=True,expected_running=True,started_at=NOW-timedelta(seconds=5))
    values.update(overrides); return StateInputs(**values)

def test_state_resolver_never_confuses_process_and_discord():
    resolver=BotStateResolver(30,60)
    assert resolver.resolve(inputs(),NOW).value=='starting'
    assert resolver.resolve(inputs(connected=True,ready=False,last_heartbeat_at=NOW),NOW).value=='starting'
    assert resolver.resolve(inputs(connected=True,ready=True,last_heartbeat_at=NOW),NOW).value=='online'
    assert resolver.resolve(inputs(started_at=NOW-timedelta(seconds=90),last_heartbeat_at=NOW-timedelta(seconds=31)),NOW).value=='disconnected'
    assert resolver.resolve(inputs(process_running=False,process_state='crashed'),NOW).value=='crashed'

def test_state_precedence():
    r=BotStateResolver(30,60)
    assert r.resolve(inputs(enabled=False,connected=True,ready=True,last_heartbeat_at=NOW),NOW).value=='disabled'
    assert r.resolve(inputs(operation='restart'),NOW).value=='restarting'
    assert r.resolve(inputs(operation='stop'),NOW).value=='stopping'
    assert r.resolve(inputs(process_running=False,process_state='offline',expected_running=False),NOW).value=='offline'

def heartbeat(bot='events',instance='INST-000020',when=None,connected=True,ready=True):
    return AgentHeartbeat(bot_id=bot,instance_id=instance,timestamp=when or datetime.now(timezone.utc),connected=connected,ready=ready,latency_ms=42,guild_count=3)

@pytest.fixture
def heartbeat_store():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False}); Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        db.add(Bot(id='events',display_name='Events',folder='.',entry_file='bot.py',management_secret_hash=hashlib.sha256(b'dedicated-secret').hexdigest()))
        db.add(BotInstance(bot_id='events',instance_id='INST-000020',state='running',expected_running=True,python_executable='python',entry_file='bot.py',working_directory='.'))
        db.commit()
    return factory

def test_authenticated_current_instance_and_ordering(heartbeat_store):
    service=HeartbeatService(heartbeat_store,60,0)
    service.accept(heartbeat(), 'dedicated-secret')
    with heartbeat_store() as db:
        row=db.scalar(select(BotInstance)); assert row.discord_ready and row.discord_connected and row.discord_latency_ms==42 and row.guild_count==3
        assert db.scalar(select(ActivityEvent).where(ActivityEvent.event_type=='BOT_READY'))
    with pytest.raises(HeartbeatRejected): service.accept(heartbeat(instance='INST-000019'),'dedicated-secret')
    old=datetime.now(timezone.utc)-timedelta(seconds=1)
    with pytest.raises(HeartbeatRejected): service.accept(heartbeat(when=old),'dedicated-secret')
    with heartbeat_store() as db: assert db.scalar(select(BotInstance)).discord_ready

def test_invalid_agent_credentials(heartbeat_store):
    service=HeartbeatService(heartbeat_store,60,0)
    for credential in (None,'wrong'):
        with pytest.raises(HeartbeatRejected) as error: service.accept(heartbeat(),credential)
        assert error.value.status_code==401

def test_disconnect_then_recovery_without_restart(heartbeat_store):
    service=HeartbeatService(heartbeat_store,60,0); resolver=BotStateResolver(30,60)
    service.accept(heartbeat(connected=False,ready=False),'dedicated-secret')
    with heartbeat_store() as db:
        row=db.scalar(select(BotInstance)); assert resolver.resolve(inputs(started_at=NOW-timedelta(seconds=90),connected=row.discord_connected,ready=row.discord_ready,last_heartbeat_at=row.last_heartbeat_at)).value=='disconnected'
    service.accept(heartbeat(),'dedicated-secret')
    with heartbeat_store() as db:
        row=db.scalar(select(BotInstance)); assert row.instance_id=='INST-000020' and resolver.resolve(inputs(connected=row.discord_connected,ready=row.discord_ready,last_heartbeat_at=row.last_heartbeat_at)).value=='online'

def test_agent_reporting_failure_is_isolated(monkeypatch):
    agent=BotManagementAgent('events','INST-000020','secret',interval=.01)
    async def fail(): raise URLError('unavailable')
    monkeypatch.setattr(agent,'send_once',fail)
    async def scenario():
        await agent.start(); await asyncio.sleep(.02); assert agent._task and not agent._task.done(); await agent.stop()
    asyncio.run(scenario())
