import io
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app.database import Base
from app.main import create_app
from app.models import Bot, BotAssignment, Effect, Permission, PlatformRole, Role, RolePermission, Session, User, UserPermission
from app.services.console import ConsoleBroker, ConsoleCapture, ConsoleSubscriptionManager, SecretRedactor
from app.services.console_stream import console_subscriptions


class FakeProcess:
    def __init__(self,stdout,stderr): self.stdout=io.BytesIO(stdout); self.stderr=io.BytesIO(stderr)


def test_capture_streams_encoding_truncation_buffer_redaction_and_rotation(tmp_path):
    secrets=['fake-app-secret-123','fake-supervisor-secret-123','fake-agent-secret-123','fake-bot-token-123']
    broker=ConsoleBroker(8); capture=ConsoleCapture(broker,SecretRedactor(secrets),tmp_path/'logs',24,500,2)
    process=FakeProcess(b'out fake-app-secret-123\ninvalid:\xff\n'+b'x'*100+b'\n',b'err fake-supervisor-secret-123 fake-agent-secret-123 fake-bot-token-123\n')
    capture.attach(process,'events','INST-000043')
    deadline=time.time()+2
    while capture._threads and time.time()<deadline: time.sleep(.01)
    records=broker.records('events'); assert len(records)<=8
    assert [x['sequence'] for x in records]==sorted(x['sequence'] for x in records)
    assert {x['stream'] for x in records}>={'stdout','stderr'} and all(x['instance_id']=='INST-000043' for x in records)
    assert any('\ufffd' in x['message'] for x in records) and any('[output truncated]' in x['message'] for x in records)
    combined='\n'.join(x['message'] for x in records); assert all(secret not in combined for secret in secrets)
    initial_log=''.join(x.read_text(errors='replace') for x in (tmp_path/'logs'/'events').glob('console.log*'))
    assert '[REDACTED]' in initial_log and all(secret not in initial_log for secret in secrets)
    for n in range(30): capture.emit('events','INST-000043','stdout',f'rotation record {n}')
    folder=(tmp_path/'logs'/'events').resolve(); assert folder.is_relative_to((tmp_path/'logs').resolve())
    files=list(folder.glob('console.log*')); assert len(files)<=3 and (folder/'console.log').exists()
    disk=''.join(x.read_text(errors='replace') for x in files); assert all(secret not in disk for secret in secrets)


def test_redactor_strips_ansi_and_preserves_untrusted_html_as_text():
    safe=SecretRedactor(['known-secret-value']).redact('\x1b[2J<script>alert(1)</script> token=known-secret-value\x00')
    assert safe=='<script>alert(1)</script> token=[REDACTED]'


def test_broker_fanout_and_slow_client_is_bounded():
    manager=ConsoleSubscriptionManager(queue_size=2,max_per_user=3,max_per_bot=3)
    one,q1=manager.subscribe(1,'events'); two,q2=manager.subscribe(2,'events')
    manager.publish('events',[{'sequence':1,'message':'one'}]); assert q1.qsize()==q2.qsize()==1
    manager.publish('events',[{'sequence':2},{'sequence':3},{'sequence':4}])
    assert q1.qsize()<=2 and q2.qsize()<=2 and manager.revoked(one) and manager.revoked(two)


@pytest.fixture
def console_app(monkeypatch):
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool); Base.metadata.create_all(engine); factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        permission=Permission(key='console.view'); view=Permission(key='bot.view'); role=Role(key='viewer',name='Viewer'); bot=Bot(id='events',display_name='Events',folder='.',entry_file='bot.py'); hidden=Bot(id='pay',display_name='Pay',folder='.',entry_file='bot.py')
        allowed=User(discord_id='2',username='alex',display_name='Alex'); denied=User(discord_id='3',username='sam',display_name='Sam'); owner=User(discord_id='1',username='owner',display_name='Owner',platform_role=PlatformRole.OWNER)
        db.add_all([permission,view,role,bot,hidden,allowed,denied,owner]); db.flush(); db.add_all([RolePermission(role_id=role.id,permission_id=permission.id),RolePermission(role_id=role.id,permission_id=view.id),BotAssignment(user_id=allowed.id,bot_id=bot.id,role_id=role.id),BotAssignment(user_id=denied.id,bot_id=bot.id,role_id=role.id),UserPermission(user_id=denied.id,bot_id=bot.id,permission_id=permission.id,effect=Effect.DENY)])
        for sid,user in [('allowed',allowed),('denied',denied),('owner',owner)]: db.add(Session(id=sid,user_id=user.id,csrf_token='csrf',expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
        db.commit(); ids={'allowed':allowed.id,'denied':denied.id,'owner':owner.id}
    app=create_app(); app.state.session_factory=factory
    class ConsoleClient:
        async def console(self,bot_id,after=0): return {'records':[]}
    monkeypatch.setattr('app.routes.process_manager.client',ConsoleClient())
    return TestClient(app),factory,ids


def test_websocket_auth_assignment_explicit_deny_owner_and_isolation(console_app):
    client,_,_=console_app
    for cookie,path in [(None,'events'),('denied','events'),('allowed','pay')]:
        with pytest.raises(WebSocketDisconnect):
            kwargs={'cookies':{'dbm_session':cookie}} if cookie else {}
            with client.websocket_connect(f'/ws/bots/{path}/console',**kwargs): pass
    for cookie,path in [('allowed','events'),('owner','pay')]:
        with client.websocket_connect(f'/ws/bots/{path}/console',cookies={'dbm_session':cookie}) as ws: assert ws.receive_json()['state']=='connected'


def test_active_permission_and_disabled_user_revocation(console_app):
    client,factory,ids=console_app
    with client.websocket_connect('/ws/bots/events/console',cookies={'dbm_session':'allowed'}) as ws:
        assert ws.receive_json()['state']=='connected'; console_subscriptions.revoke(ids['allowed'],'events')

        for _ in range(3):
            try: ws.receive_json()
            except WebSocketDisconnect: break
        else: pytest.fail('revoked WebSocket remained connected')
    with factory() as db: db.get(User,ids['allowed']).enabled=False; db.commit()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws/bots/events/console',cookies={'dbm_session':'allowed'}): pass

def test_supervisor_owns_single_capture_and_redacts_injected_agent_secret(tmp_path):
    from app.supervisor.service import SupervisorService
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool); Base.metadata.create_all(engine); factory=sessionmaker(engine,expire_on_commit=False)
    script=tmp_path/'managed.py'; script.write_text("import os,sys,time\nprint('<script>alert(1)</script> '+os.environ['BOT_MANAGEMENT_SECRET'],flush=True)\nprint('stderr-line',file=sys.stderr,flush=True)\ntime.sleep(10)\n")
    with factory() as db: db.add(Bot(id='managed',display_name='Managed',folder=str(tmp_path),entry_file=script.name,python_executable=sys.executable)); db.commit()
    broker=ConsoleBroker(20); capture=ConsoleCapture(broker,SecretRedactor(),tmp_path/'logs'); service=SupervisorService(factory,1,capture); started=service.start('managed')
    try:
        deadline=time.time()+3
        while time.time()<deadline and not {'stdout','stderr'}<=set(x['stream'] for x in broker.records('managed')): time.sleep(.02)
        records=broker.records('managed'); assert {'stdout','stderr'}<=set(x['stream'] for x in records)
        assert all(x['instance_id']==started['instance_id'] for x in records)
        text='\n'.join(x['message'] for x in records); assert '<script>alert(1)</script>' in text and '[REDACTED]' in text
        with factory() as db:
            verifier=db.get(Bot,'managed').management_secret_hash
        assert verifier not in text
    finally: service.stop('managed')

def test_console_frontend_uses_textcontent_not_html_for_messages():
    source=Path('app/templates/console.html').read_text()
    assert 'message.textContent=record.message' in source
    assert 'innerHTML' not in source
