import sqlite3
from pathlib import Path
import pytest
from sqlalchemy import select
from app.adapters.base import BaseBotAdapter, DatabaseColumn, DatabaseSource, DatabaseTable
from app.adapters.registry import register_adapter
from app.core.config import Settings
from app.models import ActivityEvent, AuditLog, Backup, BackupType, Bot, PlatformRole, User
from app.services.sqlite_data import SQLiteConflict, SQLiteDataService, SQLiteNotFound, SQLiteValidationError

class Adapter(BaseBotAdapter):
    def get_database_sources(self):
        columns=(DatabaseColumn('id',editable=False),DatabaseColumn('discord_id',type='discord_id',editable=True),DatabaseColumn('points',editable=True,minimum=0),DatabaseColumn('status',editable=True,choices=('active','disabled')),DatabaseColumn('secret',sensitive=True))
        return (DatabaseSource('events','Events Database','events.db',True,(DatabaseTable('users',editable=True,allow_insert=True,allow_delete=True,columns=columns,search_columns=('discord_id','status')),DatabaseTable('hidden',visible=False),)),DatabaseSource('readonly','Read only','read.db'))
try: register_adapter('stage6-test',Adapter())
except ValueError: pass

def setup(db,tmp_path):
    root=tmp_path/'data'; root.mkdir()
    con=sqlite3.connect(root/'events.db'); con.executescript("PRAGMA foreign_keys=ON; CREATE TABLE users(id INTEGER PRIMARY KEY, discord_id TEXT NOT NULL, points INTEGER NOT NULL CHECK(points>=0), status TEXT NOT NULL, secret TEXT); CREATE TABLE child(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id)); CREATE TABLE hidden(value TEXT); INSERT INTO users VALUES(1,'298121351871594497',120,'active','never-return-this'); INSERT INTO users VALUES(2,'2',50,'disabled','also-secret'); INSERT INTO child VALUES(1,1);"); con.commit(); con.close()
    sqlite3.connect(root/'read.db').close(); sqlite3.connect(root/'unregistered.db').close()
    user=User(discord_id='1',username='owner',display_name='Owner',platform_role=PlatformRole.OWNER); bot=Bot(id='events',display_name='Events',folder=str(tmp_path),entry_file='bot.py',data_roots=['data'],adapter='stage6-test'); db.add_all([user,bot]); db.commit()
    settings=Settings(app_secret='x'*32,environment='test',supervisor_secret='y'*32,backup_root=str(tmp_path/'backups'),backup_min_free_mb=0,database_url=f"sqlite:///{root/'platform.db'}")
    return SQLiteDataService(db,settings),user,bot,root

def test_registration_schema_browse_filter_search_injection(db,tmp_path):
    service,user,bot,root=setup(db,tmp_path)
    assert [x['id'] for x in service.overview(bot)]==['events','readonly']
    assert [x['name'] for x in service.tables(bot,'events')]==['users']
    schema=service.schema(bot,'events','users'); assert 'secret' not in [x['name'] for x in schema if not x['hidden']]
    result=service.browse(bot,'events','users',1,25,'points','desc',"' OR 1=1 --",[]); assert result['total']==0
    result=service.browse(bot,'events','users',1,25,filters=[{'column':'points','operator':'greater_than','value':60}]); assert result['total']==1 and result['rows'][0]['discord_id']=='298121351871594497' and 'secret' not in result['rows'][0]
    with pytest.raises(SQLiteValidationError): service.browse(bot,'events','users',1,25,'points; DROP TABLE users')
    with pytest.raises(SQLiteNotFound): service.schema(bot,'events','users; DROP TABLE users')
    assert sqlite3.connect(root/'events.db').execute('SELECT count(*) FROM users').fetchone()[0]==2

def test_update_backup_concurrency_audit_sensitive(db,tmp_path):
    service,user,bot,root=setup(db,tmp_path); opened=service.row(bot,'events','users',{'id':1})
    assert opened['row']['discord_id']=='298121351871594497' and 'secret' not in opened['row']
    result=service.mutate(bot,'events','users',user,'update',{'points':150},{'id':1},opened['concurrency_token'])
    assert sqlite3.connect(root/'events.db').execute('SELECT points FROM users WHERE id=1').fetchone()[0]==150
    assert db.scalar(select(Backup)).backup_type is BackupType.PRE_EDIT
    audit=db.scalar(select(AuditLog).where(AuditLog.action=='DATABASE_ROW_UPDATED')); assert audit.operation_id==result['operation_id'] and 'secret' not in str(audit.event_metadata)
    assert db.scalar(select(ActivityEvent).where(ActivityEvent.event_type=='BOT_DATABASE_CHANGED'))
    with pytest.raises(SQLiteConflict): service.mutate(bot,'events','users',user,'update',{'points':151},{'id':1},opened['concurrency_token'])
    assert sqlite3.connect(root/'events.db').execute('SELECT points FROM users WHERE id=1').fetchone()[0]==150

def test_create_delete_constraints_readonly_and_backup_failure(db,tmp_path,monkeypatch):
    service,user,bot,root=setup(db,tmp_path)
    made=service.mutate(bot,'events','users',user,'create',{'discord_id':'999999999999999999','points':5,'status':'active'})
    created=service.row(bot,'events','users',{'id':int(made['record'])}); assert created['row']['discord_id']=='999999999999999999'
    service.mutate(bot,'events','users',user,'delete',key={'id':int(made['record'])},token=created['concurrency_token'],confirmation='DELETE users')
    with pytest.raises(SQLiteValidationError): service.mutate(bot,'events','users',user,'delete',key={'id':1},token=service.row(bot,'events','users',{'id':1})['concurrency_token'],confirmation='DELETE users')
    with pytest.raises(SQLiteNotFound): service.mutate(bot,'readonly','anything',user,'create',{'x':1})
    before=sqlite3.connect(root/'events.db').execute('SELECT points FROM users WHERE id=2').fetchone()[0]; opened=service.row(bot,'events','users',{'id':2})
    monkeypatch.setattr(service.backups,'create',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('no backup')))
    with pytest.raises(Exception): service.mutate(bot,'events','users',user,'update',{'points':60},{'id':2},opened['concurrency_token'])
    assert sqlite3.connect(root/'events.db').execute('SELECT points FROM users WHERE id=2').fetchone()[0]==before

def test_platform_and_path_exclusion(db,tmp_path,monkeypatch):
    service,user,bot,root=setup(db,tmp_path)
    class Unsafe(BaseBotAdapter):
        def get_database_sources(self): return (DatabaseSource('events','Events','../outside.db'),)
    monkeypatch.setattr('app.services.sqlite_data.get_adapter',lambda _:Unsafe())
    with pytest.raises(SQLiteNotFound): service.source(bot,'events')

from app.adapters.base import BotHealth, BotState, DatabaseEditPolicy

class OfflineAdapter(Adapter):
    def get_database_sources(self):
        source=super().get_database_sources()[0]
        return (DatabaseSource(source.id,source.label,source.path,source.editable,source.tables,edit_policy=DatabaseEditPolicy.EDIT_REQUIRES_BOT_STOP),)
try: register_adapter('stage6-offline-test',OfflineAdapter())
except ValueError: pass

class FakeManager:
    def __init__(self,ready=True,stop_fails=False,start_fails=False):
        self.running=True; self.ready=ready; self.stop_fails=stop_fails; self.start_fails=start_fails; self.instance='INST-OLD'; self.actions=[]
    async def get_status(self,bot_id,enabled=True):
        state=BotState.ONLINE if self.running and self.ready else BotState.STARTING if self.running else BotState.OFFLINE
        return BotHealth(state=state,process_running=self.running,discord_connected=self.running and self.ready,discord_ready=self.running and self.ready,heartbeat_fresh=self.running and self.ready,instance_id=self.instance)
    async def stop_bot(self,bot):
        self.actions.append('stop')
        if self.stop_fails: return await self.get_status(bot.id)
        self.running=False; return await self.get_status(bot.id)
    async def start_bot(self,bot):
        self.actions.append('start')
        if self.start_fails: raise RuntimeError('start rejected')
        self.running=True; self.instance='INST-NEW'; return await self.get_status(bot.id)

def test_offline_edit_uses_supervisor_backup_new_instance_and_ready(db,tmp_path):
    service,user,bot,root=setup(db,tmp_path); bot.adapter='stage6-offline-test'; db.commit()
    manager=FakeManager(); service.manager=manager
    opened=service.row(bot,'events','users',{'id':1})
    import asyncio
    result=asyncio.run(service.mutate_process_aware(bot,'events','users',user,'update',{'points':151},{'id':1},opened['concurrency_token']))
    assert manager.actions==['stop','start']
    assert result['database_change']=='success' and result['integrity']=='valid'
    assert result['bot_restart']=='success' and result['discord_ready']=='success'
    assert result['new_instance_id']=='INST-NEW'
    assert db.scalar(select(Backup)).verification_status.value=='verified'
    types=set(db.scalars(select(ActivityEvent.event_type)))
    assert {'DATABASE_EDIT_STARTED','DATABASE_EDIT_APPLIED','DATABASE_EDIT_VALIDATED','DATABASE_EDIT_READY_CONFIRMED'} <= types

def test_offline_stop_failure_never_writes(db,tmp_path):
    service,user,bot,root=setup(db,tmp_path); bot.adapter='stage6-offline-test'; db.commit(); service.manager=FakeManager(stop_fails=True)
    opened=service.row(bot,'events','users',{'id':1})
    with pytest.raises(Exception) as caught:
        import asyncio
        asyncio.run(service.mutate_process_aware(bot,'events','users',user,'update',{'points':999},{'id':1},opened['concurrency_token']))
    assert sqlite3.connect(root/'events.db').execute('SELECT points FROM users WHERE id=1').fetchone()[0]==120
    assert caught.value.workflow_result['database_change']=='failed'
    assert service.manager.actions==['stop']

def test_database_service_has_no_direct_lifecycle_calls():
    source=Path('app/services/sqlite_data.py').read_text()
    assert 'subprocess' not in source and 'os.kill' not in source
