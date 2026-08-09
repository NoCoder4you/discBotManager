import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from app.adapters.base import BaseBotAdapter
from app.adapters.registry import register_adapter
from app.models import Bot, TaskRun, TaskSchedule
from app.scheduler.registry import TaskRegistry
from app.scheduler.schemas import ScheduleUpdate
from app.scheduler.service import SchedulerService, TaskConflict
from app.scheduler.types import RegisteredTask, ScheduleType, TaskExecutionContext, TaskResult

async def success(context:TaskExecutionContext): return TaskResult("Safe success",{"entries":18})
async def failure(context): raise RuntimeError("secret-token-must-not-leak")
async def slow(context): await asyncio.sleep(.05); return TaskResult()

class Stage7Adapter(BaseBotAdapter):
    def get_tasks(self):
        return (
            RegisteredTask("weekly_leaderboard","Weekly Leaderboard","Posts the weekly leaderboard.",success,requires_discord_ready=True),
            RegisteredTask("fixed_task","Fixed","Cannot be manually run.",success,manual_run_allowed=False,schedule_editable=False),
            RegisteredTask("failing","Failing","Test failure isolation.",failure),
            RegisteredTask("slow","Slow","Test timeout.",slow,timeout_seconds=1),
        )

try: register_adapter("stage7-test",Stage7Adapter())
except ValueError: pass

class Process:
    ready=True
    def status(self,_): return {"process_running":self.ready,"discord_ready":self.ready}

@pytest.fixture
def bot(db):
    row=Bot(id="events",display_name="Events",folder=".",entry_file="bot.py",python_executable="/usr/bin/python",adapter="stage7-test"); db.add(row); db.commit(); return row

def service(db):
    factory=lambda: db.__class__(bind=db.get_bind(),expire_on_commit=False)
    value=SchedulerService(factory,Process()); value.engine.start(paused=True); return value

def test_registry_is_explicit_and_ids_are_safe(bot):
    tasks=TaskRegistry().tasks_for(bot)
    assert [x.id for x in tasks]==["weekly_leaderboard","fixed_task","failing","slow"]
    assert not TaskRegistry().resolve(bot,"../weekly_leaderboard")
    assert not hasattr(Stage7Adapter(),"unregistered_function")

@pytest.mark.parametrize("payload",[
    {"enabled":True,"timezone":"UTC","config":{"type":"interval","every":5,"unit":"minutes"}},
    {"enabled":True,"timezone":"Europe/London","config":{"type":"daily","hour":23,"minute":59}},
    {"enabled":True,"timezone":"Europe/London","config":{"type":"weekly","weekday":6,"hour":23,"minute":59}},
    {"enabled":True,"timezone":"UTC","config":{"type":"monthly","day":28,"hour":0,"minute":0}},
    {"enabled":True,"timezone":"UTC","config":{"type":"one_time","run_at":(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}},
])
def test_structured_schedule_types(payload): assert ScheduleUpdate.model_validate(payload)

@pytest.mark.parametrize("config",[
    {"type":"interval","every":1,"unit":"minutes"},
    {"type":"daily","hour":25,"minute":0},
    {"type":"weekly","weekday":7,"hour":1,"minute":0},
    {"type":"monthly","day":31,"hour":0,"minute":0},
    {"type":"interval","every":"; rm -rf /","unit":"minutes"},
    {"type":"* * * * *"},
])
def test_invalid_raw_cron_and_injection_are_rejected(config):
    with pytest.raises(ValidationError): ScheduleUpdate.model_validate({"enabled":True,"timezone":"UTC","config":config})

def test_timezone_dst_and_reconciliation_are_stable(db,bot):
    scheduler=service(db)
    result=scheduler.configure(bot.id,"weekly_leaderboard",{"enabled":True,"timezone":"Europe/London","config":{"type":"weekly","weekday":6,"hour":1,"minute":30}})
    assert result["next_run_at"] and len(scheduler.engine.get_jobs())==1
    first=result["next_run_at"]; scheduler.reconcile()
    assert len(scheduler.engine.get_jobs())==1
    with db.__class__(bind=db.get_bind()) as check:
        assert check.scalar(select(TaskSchedule)).timezone=="Europe/London"
    assert scheduler.configure(bot.id,"weekly_leaderboard",{"enabled":True,"timezone":"Europe/London","config":{"type":"weekly","weekday":6,"hour":1,"minute":30}})["next_run_at"]==first
    scheduler.stop()

def test_execution_history_failure_skip_and_capability(db,bot):
    scheduler=service(db)
    scheduler.execute(bot.id,"weekly_leaderboard","scheduled")
    Process.ready=False; scheduler.process_service.ready=False
    scheduler.execute(bot.id,"weekly_leaderboard","scheduled")
    scheduler.execute(bot.id,"failing","scheduled")
    with db.__class__(bind=db.get_bind()) as check:
        runs=list(check.scalars(select(TaskRun).order_by(TaskRun.id)))
        assert [x.status for x in runs]==["success","skipped","failed"]
        assert runs[-1].summary=="The task raised an internal error."
        assert "secret-token" not in str(runs[-1].result_metadata)
    with pytest.raises(TaskConflict): scheduler.enqueue(bot.id,"fixed_task","manual",None,None,"Owner")
    scheduler.stop()

def test_overlap_is_per_bot_and_task(db,bot):
    scheduler=service(db); scheduler._running.add((bot.id,"weekly_leaderboard"))
    with pytest.raises(TaskConflict): scheduler.enqueue(bot.id,"weekly_leaderboard","manual",None,None,"Owner")
    assert (bot.id,"failing") not in scheduler._running
    scheduler.stop()
