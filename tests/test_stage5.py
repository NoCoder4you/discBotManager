import json, os
from pathlib import Path
import pytest
from pydantic import BaseModel
from sqlalchemy import select
from app.adapters.base import BaseBotAdapter, ConfigField, DataSource
from app.adapters.registry import register_adapter
from app.core.config import Settings
from app.models import ActivityEvent, AuditLog, Backup, BackupType, Bot, DataVersion, PlatformRole, User
from app.services.data import BotDataService, DataConflict, DataNotFound, DataValidationError

class StateModel(BaseModel):
    channel_id:str
    points:int
class Adapter(BaseBotAdapter):
    def get_data_sources(self): return (DataSource("state","Leaderboard","state.json",editable=True,validator=StateModel),DataSource("cache","Cache","cache.json"),)
    def get_config_schema(self): return (DataSource("settings","Settings","settings.json",editable=True,config_fields=(ConfigField("enabled","Enabled","boolean"),ConfigField("maximum","Maximum","integer",minimum=1,maximum=10,requires_restart=True),ConfigField("colour","Colour","colour"),ConfigField("channel_id","Channel","channel_id"),ConfigField("mode","Mode","choice",choices=("daily","weekly")),ConfigField("token","Token",sensitive=True))),)
try: register_adapter("stage5-test",Adapter())
except ValueError: pass

def setup(db,tmp_path):
    root=tmp_path/'data'; root.mkdir(); (root/'state.json').write_text('{"channel_id":"123456789012345678","points":1}',encoding='utf-8'); (root/'cache.json').write_text('{}'); (root/'settings.json').write_text(json.dumps({'enabled':True,'maximum':5,'colour':'#0388FC','channel_id':'123456789012345678','mode':'weekly','token':'secret-value'}))
    user=User(discord_id='1',username='owner',display_name='Owner',platform_role=PlatformRole.OWNER); bot=Bot(id='events',display_name='Events',folder=str(tmp_path),entry_file='bot.py',data_roots=['data'],adapter='stage5-test'); db.add_all([user,bot]); db.commit()
    settings=Settings(app_secret='x'*32,environment='test',supervisor_secret='y'*32,backup_root=str(tmp_path/'backups'),backup_min_free_mb=0)
    return BotDataService(db,settings),user,bot,root

def test_paths_sensitive_symlink_source_and_types(db,tmp_path):
    service,user,bot,root=setup(db,tmp_path); (root/'.env').write_text('TOKEN=x'); (root/'private.key').write_text('x'); (root/'bot.py').write_text('print(1)'); outside=tmp_path/'outside'; outside.write_text('no'); (root/'escape').symlink_to(outside)
    names={x['name'] for x in service.list_directory(bot)}
    assert names=={'state.json','cache.json','settings.json'}
    for path in ('../outside','../../etc/passwd','/etc/passwd','C:\\Windows\\win.ini','.env','private.key','escape'):
        with pytest.raises(DataNotFound): service.resolve(bot,path)
    with pytest.raises(DataNotFound): service.read(bot,'bot.py')

def test_json_validation_backup_atomic_history_diff_and_concurrency(db,tmp_path):
    service,user,bot,root=setup(db,tmp_path); source=service.source(bot,'state'); opened=service.read(bot,source.path)
    with pytest.raises(DataValidationError): service.save_json(bot,source,user,'{"bad":',opened['version'])
    assert not db.scalars(select(Backup)).all()
    content='{"channel_id":"123456789012345678","points":2}'
    result=service.save_json(bot,source,user,content,opened['version'])
    assert json.loads((root/'state.json').read_text())['channel_id']=='123456789012345678'
    backup=db.scalar(select(Backup)); version=db.scalar(select(DataVersion)); assert backup.backup_type is BackupType.PRE_EDIT and version.operation_id==result['operation_id']
    assert '-{"channel_id":"123456789012345678","points":1}' in service.diff(bot,source,version)
    with pytest.raises(DataConflict): service.save_json(bot,source,user,content,opened['version'])
    assert json.loads((root/'state.json').read_text())['points']==2
    assert db.scalar(select(AuditLog).where(AuditLog.action=='JSON_DATA_CHANGED')) and db.scalar(select(ActivityEvent).where(ActivityEvent.event_type=='JSON_DATA_CHANGED'))

def test_backup_failure_blocks_and_atomic_failure_preserves_old(db,tmp_path,monkeypatch):
    service,user,bot,root=setup(db,tmp_path); source=service.source(bot,'state'); opened=service.read(bot,source.path); old=(root/'state.json').read_bytes()
    monkeypatch.setattr(service.backups,'create',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('fail')))
    with pytest.raises(Exception): service.save_json(bot,source,user,'{"channel_id":"123456789012345678","points":2}',opened['version'])
    assert (root/'state.json').read_bytes()==old
    monkeypatch.undo(); backup=service.backups.create(bot,user,BackupType.PRE_EDIT,'test',protected=True); monkeypatch.setattr(service.backups,'create',lambda *a,**k: backup); monkeypatch.setattr('app.services.data.os.replace',lambda *a: (_ for _ in ()).throw(OSError('fail')))
    with pytest.raises(OSError): service.save_json(bot,source,user,'{"channel_id":"123456789012345678","points":2}',opened['version'])
    assert (root/'state.json').read_bytes()==old and not list(root.glob('*.tmp'))

def test_typed_config_secret_and_restart(db,tmp_path):
    service,user,bot,root=setup(db,tmp_path); source=service.source(bot,'settings',True); view=service.config_view(bot,source)
    assert view['values']['token']=={'configured':True} and 'secret-value' not in json.dumps(view)
    values={'enabled':False,'maximum':7,'colour':'#AABBCC','channel_id':'999999999999999999','mode':'daily'}
    result=service.save_config(bot,source,user,values,view['version']); stored=json.loads((root/'settings.json').read_text())
    assert result['restart_required'] and stored['token']=='secret-value' and stored['channel_id']=='999999999999999999'
    assert 'secret-value' not in json.dumps(db.scalar(select(AuditLog)).event_metadata)
    with pytest.raises(DataValidationError): service.validate_config(source,{'maximum':99,'channel_id':'abc','colour':'red','mode':'other'},stored)
