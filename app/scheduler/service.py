from __future__ import annotations
import asyncio, json, threading, uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, func, select
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.models import Bot, Operation, OperationStatus, TaskRun, TaskSchedule, utcnow
from app.scheduler.registry import TaskRegistry
from app.scheduler.schemas import SCHEDULE_ADAPTER
from app.scheduler.types import ConcurrencyPolicy, TaskExecutionContext, TaskResult, TaskRunStatus, TaskTrigger

class SchedulerUnavailable(RuntimeError): pass
class TaskConflict(RuntimeError): pass
class TaskUnavailable(LookupError): pass

class SchedulerService:
    """Durable supervisor-owned scheduler for trusted adapter tasks only.

    The database is authoritative. APScheduler contains deterministic, replaceable
    internal jobs and is rebuilt without executing jobs during reconciliation.
    """
    HISTORY_LIMIT=500
    def __init__(self,db_factory,process_service,registry=None):
        self.db_factory=db_factory; self.process_service=process_service; self.registry=registry or TaskRegistry()
        self.engine=BackgroundScheduler(timezone=timezone.utc,job_defaults={"coalesce":True,"max_instances":1,"misfire_grace_time":1})
        self._running:set[tuple[str,str]]=set(); self._guard=threading.Lock(); self.last_reconciliation_at=None; self.failures=[]
    @staticmethod
    def job_id(bot_id,task_id): return f"task:{bot_id}:{task_id}"
    def start(self):
        if not self.engine.running:
            self.engine.start(paused=True); self._recover_interrupted(); self.reconcile(); self.engine.resume()
    def _recover_interrupted(self):
        """A restarted supervisor cannot preserve Python workers; record that honestly."""
        with self.db_factory() as db:
            for run in db.scalars(select(TaskRun).where(TaskRun.status.in_([TaskRunStatus.QUEUED.value,TaskRunStatus.RUNNING.value]))):
                run.status=TaskRunStatus.INTERRUPTED.value; run.finished_at=utcnow(); run.summary="Task execution was interrupted by a supervisor restart."
            db.commit()
    def stop(self):
        if self.engine.running: self.engine.shutdown(wait=False)
    def _trigger(self,config,timezone_name):
        tz=ZoneInfo(timezone_name); kind=config.type
        if kind=="interval": return IntervalTrigger(**{config.unit:config.every},timezone=tz)
        if kind=="daily": return CronTrigger(hour=config.hour,minute=config.minute,timezone=tz)
        if kind=="weekly": return CronTrigger(day_of_week=config.weekday,hour=config.hour,minute=config.minute,timezone=tz)
        if kind=="monthly": return CronTrigger(day=config.day,hour=config.hour,minute=config.minute,timezone=tz)
        return DateTrigger(run_date=config.run_at)
    def reconcile(self):
        expected=set(); self.failures=[]
        with self.db_factory() as db:
            schedules=list(db.scalars(select(TaskSchedule))); bots={b.id:b for b in db.scalars(select(Bot))}
            for schedule in schedules:
                bot=bots.get(schedule.bot_id); task=self.registry.resolve(bot,schedule.task_id) if bot else None
                if not task:
                    schedule.next_run_at=None; schedule.reconciliation_required=True; continue
                schedule.reconciliation_required=False
                if not schedule.enabled or not bot.enabled:
                    schedule.next_run_at=None; continue
                try:
                    config=SCHEDULE_ADAPTER.validate_python(schedule.structured_config)
                    if config.type not in {x.value for x in task.allowed_schedule_types}: raise ValueError("Unsupported schedule type")
                    jid=self.job_id(bot.id,task.id); expected.add(jid)
                    job=self.engine.add_job(self.execute,self._trigger(config,schedule.timezone),args=(bot.id,task.id,TaskTrigger.SCHEDULED.value,None,None,"SYSTEM"),id=jid,replace_existing=True,**({"misfire_grace_time":86400} if task.misfire_policy.value=="run_once" else {"misfire_grace_time":1}))
                    schedule.next_run_at=job.next_run_time
                except Exception:
                    schedule.next_run_at=None; schedule.reconciliation_required=True; self.failures.append({"bot_id":schedule.bot_id,"task_id":schedule.task_id})
            for job in self.engine.get_jobs():
                if job.id.startswith("task:") and job.id not in expected: self.engine.remove_job(job.id)
            db.commit()
        self.last_reconciliation_at=utcnow(); return self.health()
    def configure(self,bot_id,task_id,payload):
        with self.db_factory() as db:
            bot=db.get(Bot,bot_id); task=self.registry.resolve(bot,task_id) if bot else None
            if not task: raise TaskUnavailable("Registered task is unavailable")
            config=SCHEDULE_ADAPTER.validate_python(payload["config"])
            if config.type not in {x.value for x in task.allowed_schedule_types}: raise ValueError("Schedule type is not supported by this task")
            current=db.scalar(select(TaskSchedule).where(TaskSchedule.bot_id==bot_id,TaskSchedule.task_id==task_id))
            if current and not task.schedule_editable and current.structured_config!=payload["config"]: raise TaskConflict("This task has a fixed schedule")
            if not task.disable_allowed and not payload["enabled"]: raise TaskConflict("This task cannot be disabled")
            before=None if not current else {"enabled":current.enabled,"schedule_type":current.schedule_type,"structured_config":current.structured_config,"timezone":current.timezone,"next_run_at":current.next_run_at,"reconciliation_required":current.reconciliation_required}
            row=current or TaskSchedule(bot_id=bot_id,task_id=task_id,schedule_type=config.type,structured_config=payload["config"])
            if not current: db.add(row)
            row.enabled=payload["enabled"]; row.schedule_type=config.type; row.structured_config=payload["config"]; row.timezone=payload["timezone"]; row.reconciliation_required=True; db.commit()
        self.reconcile()
        with self.db_factory() as db:
            row=db.scalar(select(TaskSchedule).where(TaskSchedule.bot_id==bot_id,TaskSchedule.task_id==task_id))
            if row.reconciliation_required:
                if before is None: db.delete(row)
                else:
                    for key,value in before.items(): setattr(row,key,value)
                db.commit(); self.reconcile()
                raise SchedulerUnavailable("The durable scheduler did not accept this schedule")
            return self.schedule_payload(row)
    def toggle(self,bot_id,task_id,enabled):
        with self.db_factory() as db:
            bot=db.get(Bot,bot_id); task=self.registry.resolve(bot,task_id) if bot else None
            row=db.scalar(select(TaskSchedule).where(TaskSchedule.bot_id==bot_id,TaskSchedule.task_id==task_id))
            if not task or not row: raise TaskUnavailable("Registered task is unavailable")
            if not task.disable_allowed and not enabled: raise TaskConflict("This task cannot be disabled")
            row.enabled=enabled; row.reconciliation_required=True; db.commit()
        self.reconcile()
        with self.db_factory() as db: return self.schedule_payload(db.scalar(select(TaskSchedule).where(TaskSchedule.bot_id==bot_id,TaskSchedule.task_id==task_id)))
    @staticmethod
    def schedule_payload(row):
        return {"enabled":row.enabled,"schedule_type":row.schedule_type,"config":row.structured_config,"timezone":row.timezone,"next_run_at":row.next_run_at.isoformat() if row.next_run_at else None,"last_run_at":row.last_run_at.isoformat() if row.last_run_at else None,"last_status":row.last_status,"status":"reconciliation_required" if row.reconciliation_required else "available"}
    def enqueue(self,bot_id,task_id,trigger,operation_id,user_id,actor):
        with self.db_factory() as db:
            bot=db.get(Bot,bot_id); task=self.registry.resolve(bot,task_id) if bot else None
            if not task: raise TaskUnavailable("Registered task is unavailable")
            if trigger==TaskTrigger.MANUAL.value and not task.manual_run_allowed: raise TaskConflict("This task cannot be run manually")
            key=(bot_id,task_id)
            with self._guard:
                if task.concurrency_policy is ConcurrencyPolicy.FORBID_OVERLAP and key in self._running: raise TaskConflict("Task is already running.")
                self._running.add(key)
            run=TaskRun(public_id=f"RUN-{uuid.uuid4()}",bot_id=bot_id,task_id=task_id,trigger=trigger,status=TaskRunStatus.QUEUED.value,triggered_by_id=user_id,actor_display=actor[:100],operation_id=operation_id); db.add(run); db.commit(); run_id=run.public_id
        threading.Thread(target=self.execute,args=(bot_id,task_id,trigger,operation_id,user_id,actor,run_id),daemon=True).start(); return {"run_id":run_id,"operation_id":operation_id,"status":"queued"}
    def execute(self,bot_id,task_id,trigger,operation_id=None,user_id=None,actor="SYSTEM",run_id=None):
        key=(bot_id,task_id); acquired=run_id is not None
        try:
            with self.db_factory() as db:
                bot=db.get(Bot,bot_id); task=self.registry.resolve(bot,task_id) if bot else None
                if not task: return
                if not acquired:
                    with self._guard:
                        if task.concurrency_policy is ConcurrencyPolicy.FORBID_OVERLAP and key in self._running:
                            return self._record_skip(db,bot_id,task_id,trigger,"Skipped because the task is already running.")
                        self._running.add(key); acquired=True
                    run=TaskRun(public_id=f"RUN-{uuid.uuid4()}",bot_id=bot_id,task_id=task_id,trigger=trigger,status="queued",actor_display="SYSTEM"); db.add(run); db.flush(); run_id=run.public_id
                else: run=db.scalar(select(TaskRun).where(TaskRun.public_id==run_id))
                reason=self._precondition(bot,task)
                if reason: return self._finish(db,run,TaskRunStatus.SKIPPED,reason,task)
                run.status="running"; run.started_at=utcnow(); self._event(db,EventType.TASK_STARTED,bot_id,task_id,run,actor); db.commit()
            context=TaskExecutionContext(bot_id,task_id,run_id,operation_id,TaskTrigger(trigger),user_id)
            try:
                result=asyncio.run(asyncio.wait_for(task.handler(context),timeout=task.timeout_seconds))
                if not isinstance(result,TaskResult): raise TypeError("Task handler returned an invalid result")
                status=TaskRunStatus.SUCCESS; summary=result.summary; details=result.details
            except asyncio.TimeoutError: status=TaskRunStatus.TIMED_OUT; summary="The task exceeded its execution timeout."; details={}
            except Exception: status=TaskRunStatus.FAILED; summary="The task raised an internal error."; details={}
            with self.db_factory() as db:
                run=db.scalar(select(TaskRun).where(TaskRun.public_id==run_id)); self._finish(db,run,status,summary,task,details,actor)
        finally:
            if acquired:
                with self._guard: self._running.discard(key)
    def _precondition(self,bot,task):
        if not bot.enabled and not task.allow_while_bot_disabled: return "Skipped because the bot registration is disabled."
        health=self.process_service.status(bot.id)
        if task.requires_discord_ready and not health.get("discord_ready"): return "Skipped because the bot was not Ready on Discord."
        if task.requires_process_running and not health.get("process_running"): return "Skipped because the bot process was not running."
        if task.requires_process_offline and health.get("process_running"): return "Skipped because the bot process was running."
    def _record_skip(self,db,bot_id,task_id,trigger,summary):
        run=TaskRun(public_id=f"RUN-{uuid.uuid4()}",bot_id=bot_id,task_id=task_id,trigger=trigger,status="queued",actor_display="SYSTEM"); db.add(run); db.flush(); return self._finish(db,run,TaskRunStatus.SKIPPED,summary,None)
    def _finish(self,db,run,status,summary,task,details=None,actor="SYSTEM"):
        now=utcnow(); run.status=status.value; run.started_at=run.started_at or now; run.finished_at=now
        started=run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
        run.duration_ms=max(0,int((now-started).total_seconds()*1000)); run.summary=str(summary)[:500]
        safe=details if isinstance(details,dict) else {}; encoded=json.dumps(safe,default=str)
        run.result_metadata=safe if len(encoded.encode())<=8192 else {"truncated":True}
        schedule=db.scalar(select(TaskSchedule).where(TaskSchedule.bot_id==run.bot_id,TaskSchedule.task_id==run.task_id))
        if schedule:
            schedule.last_run_at=now; schedule.last_status=status.value
            if schedule.schedule_type=="one_time" and status is TaskRunStatus.SUCCESS: schedule.enabled=False; schedule.next_run_at=None
            elif schedule.enabled:
                job=self.engine.get_job(self.job_id(run.bot_id,run.task_id)); schedule.next_run_at=job.next_run_time if job else None
        if run.operation_id:
            operation=db.scalar(select(Operation).where(Operation.public_id==run.operation_id))
            if operation:
                operation.status=OperationStatus.COMPLETED if status in {TaskRunStatus.SUCCESS,TaskRunStatus.SKIPPED} else OperationStatus.FAILED
                operation.completed_at=now; operation.error=None if operation.status is OperationStatus.COMPLETED else run.summary
        event={TaskRunStatus.SUCCESS:EventType.TASK_COMPLETED,TaskRunStatus.FAILED:EventType.TASK_FAILED,TaskRunStatus.TIMED_OUT:EventType.TASK_TIMED_OUT,TaskRunStatus.SKIPPED:EventType.TASK_SKIPPED}[status]
        self._event(db,event,run.bot_id,run.task_id,run,actor); db.flush()
        old=list(db.scalars(select(TaskRun.id).where(TaskRun.bot_id==run.bot_id,TaskRun.task_id==run.task_id).order_by(TaskRun.id.desc()).offset(self.HISTORY_LIMIT)))
        if old: db.execute(delete(TaskRun).where(TaskRun.id.in_(old)))
        db.commit(); return run
    @staticmethod
    def _event(db,event_type,bot_id,task_id,run,actor):
        event=DomainEvent(event_type,None,bot_id,{"source":actor,"task_id":task_id,"run_id":run.public_id,"operation_id":run.operation_id,"trigger":run.trigger,"status":run.status})
        EventBus(db).publish(event); AuditService(db).record(event,run.status,task_id,run.operation_id)
    def health(self):
        with self.db_factory() as db:
            return {"status":"degraded" if self.failures else "healthy","registered_tasks":sum(len(self.registry.tasks_for(bot)) for bot in db.scalars(select(Bot))),"enabled_schedules":db.scalar(select(func.count(TaskSchedule.id)).where(TaskSchedule.enabled.is_(True))) or 0,"last_reconciliation_at":self.last_reconciliation_at.isoformat() if self.last_reconciliation_at else None,"orphaned_or_failed":len(self.failures)}
