import asyncio
import sys
from pathlib import Path
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from app.models import ActivityEvent, AuditLog, Bot, BotAssignment, Effect, Operation, PlatformRole, Role, User, UserPermission
from app.schemas import AssignmentMutation, BotMutation, PermissionOverrideMutation
from app.services.admin import AdminError, AdminService
from app.services.catalog import ROLE_MAPPINGS, seed_catalog
from app.services.permissions import PermissionService
from app.services.process_manager import BotProcessManager, ProcessConflict
from app.supervisor.service import SupervisorConflict, SupervisorService
from sqlalchemy.orm import sessionmaker

class DirectClient:
    def __init__(self,service): self.service=service
    async def status(self,bot_id): return self.service.status(bot_id)
    async def action(self,bot_id,action):
        try: return getattr(self.service,action)(bot_id)
        except SupervisorConflict as exc: raise ProcessConflict(str(exc)) from exc
    async def health(self): return self.service.health()
    async def reconcile(self): return self.service.reconcile()


def users(db):
    owner=User(discord_id="1",username="owner",display_name="Owner",platform_role=PlatformRole.OWNER)
    user=User(discord_id="2",username="user",display_name="User")
    db.add_all([owner,user]); db.commit(); return owner,user


def test_catalog_seed_is_idempotent_and_roles_are_isolated(db):
    seed_catalog(db); seed_catalog(db)
    assert db.scalar(select(func.count()).select_from(Role)) == 3
    roles={r.key:r for r in db.scalars(select(Role))}
    assert ROLE_MAPPINGS["viewer"] < ROLE_MAPPINGS["operator"] < ROLE_MAPPINGS["administrator"]
    assert roles.keys()==ROLE_MAPPINGS.keys()


def test_assignment_disable_and_cross_bot_scope(db):
    seed_catalog(db); owner,user=users(db); role=db.scalar(select(Role).where(Role.key=="operator"))
    a=Bot(id="events",display_name="Events",folder=".",entry_file="x.py",owner_id=owner.id)
    b=Bot(id="pay",display_name="Pay",folder=".",entry_file="x.py",owner_id=owner.id)
    db.add_all([a,b]); db.flush(); assignment=BotAssignment(user_id=user.id,bot_id=a.id,role_id=role.id); db.add(assignment); db.commit()
    permissions=PermissionService(db)
    assert permissions.has(user,"bot.restart",a.id)
    assert not permissions.has(user,"bot.restart",b.id)
    assignment.enabled=False; db.commit()
    assert not permissions.has(user,"bot.view",a.id) and a not in permissions.visible_bots(user)


def test_override_audit_event_and_fallback(db):
    seed_catalog(db); owner,user=users(db); bot=Bot(id="events",display_name="Events",folder=".",entry_file="x.py",owner_id=owner.id); db.add(bot); db.commit()
    service=AdminService(db); service.assign(owner,user,AssignmentMutation(bot_id=bot.id,role_key="operator"))
    service.override(owner,user,bot.id,PermissionOverrideMutation(permission_key="bot.restart",state="deny"))
    assert not PermissionService(db).has(user,"bot.restart",bot.id)
    service.override(owner,user,bot.id,PermissionOverrideMutation(permission_key="bot.restart",state="inherit"))
    assert PermissionService(db).has(user,"bot.restart",bot.id)
    assert db.scalar(select(func.count()).select_from(Operation))==3
    assert db.scalar(select(func.count()).select_from(AuditLog))==3
    assert db.scalar(select(func.count()).select_from(ActivityEvent))==3


def test_owner_protected_and_invalid_bot_input(db):
    owner,_=users(db)
    with pytest.raises(AdminError): AdminService(db).set_user_enabled(owner,owner,False)
    with pytest.raises(ValidationError): BotMutation(id="../bad",display_name="Bad",folder=".",entry_file="x",python_executable=sys.executable)


def test_path_escape_rejected(db,tmp_path,monkeypatch):
    owner,_=users(db); monkeypatch.setenv("BOT_ROOT",str(tmp_path)); from app.core.config import get_settings; get_settings.cache_clear()
    outside=tmp_path.parent/"outside.py"; outside.write_text("pass")
    data=BotMutation(id="safe",display_name="Safe",folder=str(tmp_path),entry_file="../outside.py",python_executable=sys.executable)
    with pytest.raises(AdminError): AdminService(db).create_bot(owner,data)
    get_settings.cache_clear()


def test_process_lifecycle_and_duplicate_start(tmp_path,db):
    script=tmp_path/"bot.py"; script.write_text("import time\ntime.sleep(30)\n")
    bot=Bot(id="real",display_name="Real",folder=str(tmp_path),entry_file="bot.py",python_executable=sys.executable,enabled=True); db.add(bot); db.commit()
    service=SupervisorService(lambda: db,1)
    async def scenario():
        manager=BotProcessManager(DirectClient(service)); started=await manager.start_bot(bot)
        assert started.process_running and started.pid
        with pytest.raises(ProcessConflict): await manager.start_bot(bot)
        restarted=await manager.restart_bot(bot); assert restarted.process_running
        stopped=await manager.stop_bot(bot); assert not stopped.process_running and stopped.state.value=="offline"
    asyncio.run(scenario())
