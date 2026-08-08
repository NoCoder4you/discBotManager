import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import create_app
from app.models import Bot, BotAssignment, BotInstance, Permission, PlatformRole, Role, Session, User
from app.services.catalog import seed_catalog
from app.services.telemetry import TelemetryCollector, TelemetrySample, TelemetryStore, format_bytes, format_uptime
from app.supervisor.service import SupervisorService


def test_store_is_bounded_instance_aware_and_filters_history():
    store=TelemetryStore(3); now=datetime.now(timezone.utc)
    for index in range(5): store.add(TelemetrySample((now-timedelta(minutes=4-index)).isoformat(), 'events','INST-A' if index<4 else 'INST-B',float(index),100+index,index))
    history=store.history('events',2,now); assert [x.cpu_percent for x in history]==[2,3,4]
    assert store.latest('events').instance_id=='INST-B' and store.latest('events','INST-A') is None
    assert len(store._history['events'])==3
    store.clear_current('events','INST-A'); assert store.latest('events') is not None
    store.clear_current('events','INST-B'); assert store.latest('events') is None and len(store._history['events'])==3


def test_human_formatters():
    assert format_bytes(88080384)=='84.0 MB'; assert format_bytes(None)=='—'
    assert format_uptime(42)=='42s'; assert format_uptime(494)=='8m 14s'; assert format_uptime(23460)=='6h 31m'; assert format_uptime(309600)=='3d 14h'


def test_collector_samples_validated_process_adopts_and_resets_on_restart(tmp_path):
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool); Base.metadata.create_all(engine); factory=sessionmaker(engine,expire_on_commit=False)
    script=tmp_path/'worker.py'; script.write_text('import time\nend=time.time()+30\nwhile time.time()<end: sum(i*i for i in range(10000))\n')
    with factory() as db: db.add(Bot(id='worker',display_name='Worker',folder=str(tmp_path),entry_file=script.name,python_executable=sys.executable)); db.commit()
    supervisor=SupervisorService(factory,1); first=supervisor.start('worker'); collector=TelemetryCollector(factory,SupervisorService._identity_valid,.05,1,.2)
    try:
        collector.collect_once(); time.sleep(.08); collector.collect_once(); current=collector.store.latest('worker')
        assert current and current.instance_id==first['instance_id'] and current.cpu_percent>=0 and isinstance(current.rss_bytes,int) and current.rss_bytes>0 and current.uptime_seconds>0
        adopted=TelemetryCollector(factory,SupervisorService._identity_valid,.05,1,.2); adopted.collect_once(); time.sleep(.06); adopted.collect_once()
        assert adopted.store.latest('worker').instance_id==first['instance_id'] and adopted.store.latest('worker').uptime_seconds>=current.uptime_seconds
        second=supervisor.restart('worker'); collector.collect_once(); assert collector.store.latest('worker') is None
        time.sleep(.06); collector.collect_once(); assert collector.store.latest('worker').instance_id==second['instance_id'] and collector.store.latest('worker').uptime_seconds<current.uptime_seconds+1
        supervisor.stop('worker'); collector.collect_once(); assert collector.store.latest('worker') is None
    finally:
        try: supervisor.stop('worker')
        except Exception: pass


def test_identity_failure_and_collector_failure_clear_or_preserve_isolation(db):
    bot=Bot(id='events',display_name='Events',folder='.',entry_file='x.py'); row=BotInstance(bot_id='events',instance_id='INST-X',pid=999999,process_created_at=datetime.now(timezone.utc),state='running',expected_running=True,python_executable=sys.executable,entry_file='x.py',working_directory='.')
    db.add_all([bot,row]); db.commit(); collector=TelemetryCollector(lambda:db,lambda _:False,1,1,3)
    collector.store.add(TelemetrySample(datetime.now(timezone.utc).isoformat(),'events','INST-X',1,2,3)); collector.collect_once(); assert collector.store.latest('events') is None
    class BrokenStore:
        def __enter__(self): raise RuntimeError('database unavailable')
        def __exit__(self,*_): pass
    collector=TelemetryCollector(lambda:BrokenStore(),lambda _:True,1,1,3); collector.collect_once()  # must not escape


@pytest.fixture
def telemetry_app(monkeypatch):
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool); Base.metadata.create_all(engine); factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        seed_catalog(db); role=db.scalar(select(Role).where(Role.key=='viewer'))
        events=Bot(id='events',display_name='Events',folder='.',entry_file='x.py'); pay=Bot(id='pay',display_name='Pay',folder='.',entry_file='x.py'); user=User(discord_id='2',username='alex',display_name='Alex'); disabled=User(discord_id='3',username='off',display_name='Off',enabled=False); owner=User(discord_id='1',username='owner',display_name='Owner',platform_role=PlatformRole.OWNER)
        db.add_all([events,pay,user,disabled,owner]); db.flush(); db.add(BotAssignment(user_id=user.id,bot_id=events.id,role_id=role.id))
        for sid,target in [('user',user),('disabled',disabled),('owner',owner)]: db.add(Session(id=sid,user_id=target.id,csrf_token='csrf',expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
        db.commit()
    app=create_app(); app.state.session_factory=factory
    def override_db():
        with factory() as db: yield db
    app.dependency_overrides[get_db]=override_db
    class TelemetryClient:
        async def telemetry(self,bot_id): return {'available':True,'stale':False,'sample':{'bot_id':bot_id,'instance_id':'INST-X','timestamp':datetime.now(timezone.utc).isoformat(),'cpu_percent':2.4,'rss_bytes':1024,'uptime_seconds':42}}
        async def telemetry_history(self,bot_id,minutes): return {'minutes':minutes,'samples':[{'bot_id':bot_id}]}
    monkeypatch.setattr('app.routes.process_manager.client',TelemetryClient()); return TestClient(app)


def test_telemetry_api_permissions_history_bounds_and_isolation(telemetry_app):
    client=telemetry_app
    assert client.get('/api/bots/events/telemetry').status_code==401
    assert client.get('/api/bots/events/telemetry',cookies={'dbm_session':'disabled'}).status_code==401
    assert client.get('/api/bots/pay/telemetry',cookies={'dbm_session':'user'}).status_code==404
    allowed=client.get('/api/bots/events/telemetry',cookies={'dbm_session':'user'}); assert allowed.status_code==200 and allowed.json()['sample']['bot_id']=='events'
    assert client.get('/api/bots/pay/telemetry',cookies={'dbm_session':'owner'}).status_code==200
    history=client.get('/api/bots/events/telemetry/history?minutes=999999',cookies={'dbm_session':'user'}); assert history.status_code==422
    history=client.get('/api/bots/events/telemetry/history?minutes=999',cookies={'dbm_session':'user'}); assert history.status_code==200 and history.json()['minutes']==60
