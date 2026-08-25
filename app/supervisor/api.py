import hmac
import os
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from app.core.config import get_settings
from app.database import SessionLocal
from app.supervisor.service import BotNotRegistered, IdentityMismatch, SupervisorConflict, SupervisorService
from app.schemas import AgentHeartbeat, DiscordSnapshotEnvelope
from app.services.guild_snapshots import GuildSnapshotService, SnapshotRejected
from app.services.heartbeat import HeartbeatRejected, HeartbeatService
from app.services.console import ConsoleBroker, ConsoleCapture, SecretRedactor
from app.services.telemetry import TelemetryCollector
from app.scheduler.service import SchedulerService, SchedulerUnavailable, TaskConflict, TaskUnavailable
from app.scheduler.schemas import ScheduleToggle, ScheduleUpdate, SupervisorTaskRun

settings=get_settings()
known=[settings.app_secret,settings.supervisor_secret,settings.discord_client_secret,settings.database_url]
known.extend(value for key,value in os.environ.items() if any(word in key.upper() for word in ("TOKEN","SECRET","PASSWORD","API_KEY")))
console_broker=ConsoleBroker(settings.console_buffer_lines)
console_capture=ConsoleCapture(console_broker,SecretRedactor(known),settings.console_log_root,settings.console_max_line_length,settings.console_log_max_bytes,settings.console_log_backup_count)
service=SupervisorService(SessionLocal,settings.supervisor_stop_timeout,console_capture)
scheduler_service=SchedulerService(SessionLocal,service)
telemetry_collector=TelemetryCollector(SessionLocal,SupervisorService._identity_valid,settings.telemetry_interval_seconds,settings.telemetry_history_minutes,settings.telemetry_stale_after_seconds)
heartbeat_service=HeartbeatService(SessionLocal,settings.bot_heartbeat_clock_skew_seconds,settings.bot_heartbeat_min_interval_seconds,settings.bot_heartbeat_timeout_seconds)
snapshot_refresh_requests={}
snapshot_refresh_last={}
@asynccontextmanager
async def lifespan(_):
    telemetry_collector.start(); scheduler_service.start()
    try: yield
    finally: telemetry_collector.stop(); scheduler_service.stop()
app=FastAPI(title="Bot Process Supervisor",docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

def authenticated(x_supervisor_secret: str | None=Header(None)):
    if not settings.supervisor_secret or not x_supervisor_secret or not hmac.compare_digest(x_supervisor_secret,settings.supervisor_secret): raise HTTPException(401,"Invalid supervisor credential")

def call(method,*args):
    try: return getattr(service,method)(*args)
    except BotNotRegistered as exc: raise HTTPException(404,str(exc)) from exc
    except (SupervisorConflict,IdentityMismatch) as exc: raise HTTPException(409,str(exc)) from exc

@app.post("/internal/agent/heartbeat")
async def heartbeat(request:Request,payload:AgentHeartbeat,x_bot_management_secret:str|None=Header(None)):
    if request.headers.get("content-length") and int(request.headers["content-length"])>4096: raise HTTPException(413,"Heartbeat payload too large")
    try:
        result=heartbeat_service.accept(payload,x_bot_management_secret)
        if snapshot_refresh_requests.pop(payload.bot_id,None): result["guild_snapshot_requested"]=True
        return result
    except HeartbeatRejected as exc: raise HTTPException(exc.status_code,str(exc)) from exc
@app.post("/internal/agent/guild-snapshot")
async def guild_snapshot(request:Request,payload:DiscordSnapshotEnvelope,x_bot_management_secret:str|None=Header(None)):
    if request.headers.get("content-length") and int(request.headers["content-length"])>settings.discord_snapshot_max_bytes: raise HTTPException(413,"Snapshot payload too large")
    if any(len(g.channels)>settings.discord_snapshot_max_channels or len(g.roles)>settings.discord_snapshot_max_roles for g in payload.guilds): raise HTTPException(413,"Snapshot record limit exceeded")
    with SessionLocal() as db:
        try: return GuildSnapshotService(db,settings).accept(payload,x_bot_management_secret)
        except SnapshotRejected as exc: raise HTTPException(exc.status_code,str(exc)) from exc

@app.get("/internal/health",dependencies=[Depends(authenticated)])
def health(): return service.health()
@app.post("/internal/bots/{bot_id}/guild-snapshot/refresh",dependencies=[Depends(authenticated)])
def request_guild_snapshot(bot_id:str):
    current=call("registered_instance",bot_id)
    if not current["enabled"] or not current["process_expected"]: raise HTTPException(409,"Bot process is unavailable")
    import time
    last=snapshot_refresh_last.get(bot_id)
    if last and time.monotonic()-last<settings.discord_snapshot_manual_cooldown_seconds: raise HTTPException(429,"Snapshot refresh rate limit exceeded")
    snapshot_refresh_last[bot_id]=time.monotonic(); snapshot_refresh_requests[bot_id]=True; return {"accepted":True,"status":"REQUESTED"}
@app.get("/internal/scheduler/health",dependencies=[Depends(authenticated)])
def scheduler_health(): return scheduler_service.health()
@app.post("/internal/scheduler/reconcile",dependencies=[Depends(authenticated)])
def scheduler_reconcile(): return scheduler_service.reconcile()
@app.put("/internal/scheduler/bots/{bot_id}/tasks/{task_id}",dependencies=[Depends(authenticated)])
def schedule_configure(bot_id:str,task_id:str,payload:ScheduleUpdate):
    try: return scheduler_service.configure(bot_id,task_id,payload.model_dump(mode="json"))
    except TaskUnavailable as exc: raise HTTPException(404,"Resource unavailable") from exc
    except (TaskConflict,ValueError,SchedulerUnavailable) as exc: raise HTTPException(409,str(exc)) from exc
@app.patch("/internal/scheduler/bots/{bot_id}/tasks/{task_id}",dependencies=[Depends(authenticated)])
def schedule_toggle(bot_id:str,task_id:str,payload:ScheduleToggle):
    try: return scheduler_service.toggle(bot_id,task_id,payload.enabled)
    except TaskUnavailable as exc: raise HTTPException(404,"Resource unavailable") from exc
    except (TaskConflict,SchedulerUnavailable) as exc: raise HTTPException(409,str(exc)) from exc
@app.post("/internal/scheduler/bots/{bot_id}/tasks/{task_id}/runs",dependencies=[Depends(authenticated)])
def task_run(bot_id:str,task_id:str,payload:SupervisorTaskRun):
    try: return scheduler_service.enqueue(bot_id,task_id,payload.trigger,payload.operation_id,payload.user_id,payload.actor)
    except TaskUnavailable as exc: raise HTTPException(404,"Resource unavailable") from exc
    except TaskConflict as exc: raise HTTPException(409,str(exc)) from exc
@app.get("/internal/processes",dependencies=[Depends(authenticated)])
def processes(): return call("reconcile")
@app.get("/internal/bots/{bot_id}",dependencies=[Depends(authenticated)])
def status(bot_id:str): return call("status",bot_id)
@app.get("/internal/bots/{bot_id}/maintenance",dependencies=[Depends(authenticated)])
def maintenance_state(bot_id:str):
    from app.models import Bot, BotMaintenance
    with SessionLocal() as db:
        if not db.get(Bot,bot_id): raise HTTPException(404,"Resource unavailable")
        row=db.get(BotMaintenance,bot_id)
        return {"enabled":bool(row and row.enabled),"applied_enabled":row.applied_enabled if row else None,"applied_instance_id":row.applied_instance_id if row else None}
@app.get("/internal/bots/{bot_id}/console",dependencies=[Depends(authenticated)])
def console(bot_id:str,after:int=0,limit:int=1000):
    call("status",bot_id)
    return {"stream_id":console_broker.stream_id,"records":console_broker.records(bot_id,max(0,after),min(max(1,limit),1000)),"capture_available":bot_id in console_capture.persistence_available}
@app.get("/internal/bots/{bot_id}/telemetry",dependencies=[Depends(authenticated)])
def telemetry(bot_id:str):
    current=call("registered_instance",bot_id)
    if not current["enabled"] or not current["process_expected"]:
        telemetry_collector.store.clear_current(bot_id,current["instance_id"]); return {"available":False,"stale":False,"sample":None}
    return telemetry_collector.current_payload(bot_id,current["instance_id"])
@app.get("/internal/bots/{bot_id}/telemetry/history",dependencies=[Depends(authenticated)])
def telemetry_history(bot_id:str,minutes:int=60):
    call("registered_instance",bot_id); window=min(max(1,minutes),settings.telemetry_history_minutes)
    return {"minutes":window,"samples":telemetry_collector.history_payload(bot_id,window)}
@app.post("/internal/bots/{bot_id}/start",dependencies=[Depends(authenticated)])
def start(bot_id:str): return call("start",bot_id)
@app.post("/internal/bots/{bot_id}/stop",dependencies=[Depends(authenticated)])
def stop(bot_id:str): return call("stop",bot_id)
@app.post("/internal/bots/{bot_id}/restart",dependencies=[Depends(authenticated)])
def restart(bot_id:str): return call("restart",bot_id)
@app.post("/internal/reconcile",dependencies=[Depends(authenticated)])
def reconcile(): return call("reconcile")
