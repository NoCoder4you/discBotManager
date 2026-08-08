import asyncio
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models import Bot, BotInstance
from app.services.process_manager import BotProcessManager, ProcessConflict, SupervisorUnavailable
from app.supervisor.service import SupervisorConflict, SupervisorService
from app.supervisor.api import app as supervisor_app
class DirectClient:
    def __init__(self,service): self.service=service
    async def status(self,bot_id): return self.service.status(bot_id)
    async def action(self,bot_id,action): return getattr(self.service,action)(bot_id)
    async def health(self): return self.service.health()
    async def reconcile(self): return self.service.reconcile()

def registered(db,tmp_path,name="durable"):
    script=tmp_path/f"{name}.py"; script.write_text("import time\ntime.sleep(60)\n")
    bot=Bot(id=name,display_name=name,folder=str(tmp_path),entry_file=script.name,python_executable=sys.executable,enabled=True); db.add(bot); db.commit(); return bot

def test_internal_authentication_required_and_invalid_rejected():
    client=TestClient(supervisor_app)
    assert client.get("/internal/health").status_code==401
    assert client.get("/internal/health",headers={"X-Supervisor-Secret":"invalid"}).status_code==401

def test_fastapi_and_supervisor_restart_adopt_same_process(db,tmp_path):
    bot=registered(db,tmp_path); service=SupervisorService(lambda:db,1); first=service.start(bot.id)
    try:
        time.sleep(.05); manager_after_app_restart=BotProcessManager(DirectClient(service)); health=asyncio.run(manager_after_app_restart.get_status(bot.id))
        assert health.pid==first["pid"] and health.instance_id==first["instance_id"] and health.uptime_seconds>0
        restarted_supervisor=SupervisorService(lambda:db,1); adopted=restarted_supervisor.status(bot.id)
        assert adopted["pid"]==first["pid"] and adopted["instance_id"]==first["instance_id"]
        try: restarted_supervisor.start(bot.id)
        except SupervisorConflict: pass
        assert len(list(db.scalars(select(BotInstance))))==1
    finally: service.stop(bot.id)

def test_pid_only_and_wrong_creation_time_are_rejected(db,tmp_path):
    bot=registered(db,tmp_path,"identity"); service=SupervisorService(lambda:db,1); running=service.start(bot.id)
    try:
        row=db.scalar(select(BotInstance).where(BotInstance.instance_id==running["instance_id"])); row.process_created_at=datetime(2000,1,1,tzinfo=timezone.utc); db.commit()
        reconciled=service.status(bot.id); assert reconciled["state"]=="crashed"
        # Conservative reconciliation never terminates an identity mismatch.
        import psutil; assert psutil.pid_exists(running["pid"])
    finally:
        import psutil
        if psutil.pid_exists(running["pid"]): psutil.Process(running["pid"]).terminate(); psutil.Process(running["pid"]).wait(2)

def test_expected_state_reconciliation(db,tmp_path):
    bot=registered(db,tmp_path,"crash"); service=SupervisorService(lambda:db,.2); running=service.start(bot.id)
    import psutil; process=psutil.Process(running["pid"]); process.kill(); process.wait(2)
    assert service.status(bot.id)["state"]=="crashed"
    row=db.scalar(select(BotInstance).where(BotInstance.bot_id==bot.id)); row.expected_running=False; row.state="offline"; db.commit()
    assert service.status(bot.id)["state"]=="offline"

def test_restart_creates_new_generation(db,tmp_path):
    bot=registered(db,tmp_path,"generation"); service=SupervisorService(lambda:db,1); old=service.start(bot.id)
    try:
        new=service.restart(bot.id); assert new["instance_id"]!=old["instance_id"] and new["pid"]!=old["pid"]
        rows=list(db.scalars(select(BotInstance).where(BotInstance.bot_id==bot.id).order_by(BotInstance.id))); assert rows[0].ended_at and rows[0].state=="offline" and rows[1].state=="running"
    finally: service.stop(bot.id)

def test_operation_lock_rejects_concurrent_action(db,tmp_path):
    bot=registered(db,tmp_path,"locked"); service=SupervisorService(lambda:db,1); lock=service._lock(bot.id); lock.acquire()
    try:
        try: service.start(bot.id); assert False
        except SupervisorConflict as exc: assert "already in progress" in str(exc)
    finally: lock.release()

def test_supervisor_unavailable_is_unknown():
    class Missing:
        async def status(self,_): raise SupervisorUnavailable()
    health=asyncio.run(BotProcessManager(Missing()).get_status("hidden")); assert health.state.value=="unknown" and not health.supervisor_available
