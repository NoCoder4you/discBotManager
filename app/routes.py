import asyncio
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user, requires_bot_permission, requires_owner
from app.database import get_db
from app.models import Backup, Bot, Incident, IncidentSeverity, IncidentStatus, PlatformRole, User, VerificationStatus
from sqlalchemy import func, select
from app.services.permissions import PermissionService
from app.core.security import session_from_request
from app.core.security import require_csrf
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.models import OperationStatus, utcnow
from app.services.process_manager import ProcessConflict, ProcessLaunchError, SupervisorUnavailable, process_manager, process_workflow_locks
from app.services.console import SlowSubscriber
from app.services.console_stream import console_subscriptions
from app.database import SessionLocal
from app.models import Session as UserSession
from app.core.config import get_settings
router=APIRouter()
@router.get("/",response_class=HTMLResponse)
def home(request:Request): return request.app.state.templates.TemplateResponse(request,"login.html",{})
@router.get("/dashboard",response_class=HTMLResponse)
def dashboard(request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    bots=PermissionService(db).visible_bots(user); session=session_from_request(request,db); backup_health=None
    if user.platform_role is PlatformRole.OWNER:
        protected=db.scalar(select(func.count(func.distinct(Backup.bot_id))).where(Backup.verification_status==VerificationStatus.VERIFIED)) or 0
        failed=db.scalar(select(func.count(Backup.id)).where(Backup.verification_status==VerificationStatus.FAILED)) or 0
        latest=db.scalar(select(func.max(Backup.created_at)).where(Backup.verification_status==VerificationStatus.VERIFIED))
        backup_health={"protected":protected,"total":len(bots),"failed":failed,"latest":latest}
    incident_bot_ids=[bot.id for bot in bots if PermissionService(db).has(user,"errors.view",bot.id)]
    incident_filter=[] if user.platform_role is PlatformRole.OWNER else [Incident.bot_id.in_(incident_bot_ids)]
    incident_health={"open":db.scalar(select(func.count(Incident.id)).where(Incident.status==IncidentStatus.OPEN,*incident_filter)) or 0,"high":db.scalar(select(func.count(Incident.id)).where(Incident.status==IncidentStatus.OPEN,Incident.severity==IncidentSeverity.HIGH,*incident_filter)) or 0,"critical":db.scalar(select(func.count(Incident.id)).where(Incident.status==IncidentStatus.OPEN,Incident.severity==IncidentSeverity.CRITICAL,*incident_filter)) or 0}
    return request.app.state.templates.TemplateResponse(request,"dashboard.html",{"user":user,"bots":bots,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token,"backup_health":backup_health,"incident_health":incident_health})
@router.get("/bots/{bot_id}",response_class=HTMLResponse)
def bot_detail(request:Request,bot:Bot=Depends(requires_bot_permission("bot.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    session=session_from_request(request,db); permissions={key:PermissionService(db).has(user,key,bot.id) for key in ("bot.start","bot.stop","bot.restart","bot.maintenance.enable","bot.maintenance.disable","console.view","backups.view","files.view","config.view","database.view","scheduler.view","errors.view","servers.view")}; incident_count=db.scalar(select(func.count(Incident.id)).where(Incident.bot_id==bot.id,Incident.status==IncidentStatus.OPEN)) if permissions["errors.view"] else None; latest_incident=db.scalar(select(Incident).where(Incident.bot_id==bot.id).order_by(Incident.started_at.desc())) if permissions["errors.view"] else None; return request.app.state.templates.TemplateResponse(request,"bot.html",{"user":user,"bot":bot,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token,"permissions":permissions,"incident_count":incident_count,"latest_incident":latest_incident})
@router.get("/bots/{bot_id}/console",response_class=HTMLResponse)
def console_page(request:Request,bot:Bot=Depends(requires_bot_permission("console.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    session=session_from_request(request,db); return request.app.state.templates.TemplateResponse(request,"console.html",{"user":user,"bot":bot,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token})

def _console_access(session_id,bot_id,session_factory=SessionLocal):
    with session_factory() as db:
        session=db.get(UserSession,session_id) if session_id else None
        expires=session.expires_at.replace(tzinfo=timezone.utc) if session and session.expires_at.tzinfo is None else (session.expires_at if session else None)
        if not session or not session.user or not session.user.enabled or expires<=datetime.now(timezone.utc): return None
        bot=PermissionService(db).visible_bot(session.user,bot_id)
        return session.user.id if bot and PermissionService(db).has(session.user,"console.view",bot_id) else None

@router.websocket("/ws/bots/{bot_id}/console")
async def console_socket(websocket:WebSocket,bot_id:str):
    session_id=websocket.cookies.get("dbm_session"); factory=websocket.app.state.session_factory; user_id=_console_access(session_id,bot_id,factory)
    if user_id is None: await websocket.close(code=4404,reason="Resource unavailable"); return
    try: token,queue=console_subscriptions.subscribe(user_id,bot_id)
    except SlowSubscriber: await websocket.close(code=4429,reason="Connection limit reached"); return
    await websocket.accept(); await websocket.send_json({"type":"connection","state":"connected"})
    after=0; stream_id=None; checked=0.0; capture_state=None; settings=get_settings()
    try:
        while True:
            if console_subscriptions.revoked(token): await websocket.close(code=4403,reason="Permission revoked"); return
            now=asyncio.get_running_loop().time()
            if now-checked>=settings.console_permission_revalidate_seconds:
                checked=now
                if _console_access(session_id,bot_id,factory)!=user_id: await websocket.send_json({"type":"connection","state":"permission_revoked"}); await websocket.close(code=4403,reason="Permission revoked"); return
            try:
                payload=await process_manager.client.console(bot_id,after); current_stream=payload.get("stream_id")
                if stream_id is not None and current_stream!=stream_id: after=0; payload=await process_manager.client.console(bot_id,0)
                stream_id=payload.get("stream_id"); records=payload.get("records",[]); available=payload.get("capture_available",False)
                if available!=capture_state: capture_state=available; await websocket.send_json({"type":"connection","state":"connected" if available else "console_unavailable"})
                if records: after=max(after,max(x["sequence"] for x in records)); console_subscriptions.publish(bot_id,records,stream_id)
            except Exception:
                if capture_state is not False: capture_state=False; await websocket.send_json({"type":"connection","state":"console_unavailable"})
            try: record=await asyncio.wait_for(queue.get(),.25); await websocket.send_json(record)
            except asyncio.TimeoutError: pass
    except WebSocketDisconnect: pass
    finally: console_subscriptions.unsubscribe(token)
@router.get("/api/bots/{bot_id}/status")
async def bot_status(bot:Bot=Depends(requires_bot_permission("bot.view"))):
    health=await process_manager.get_status(bot.id,bot.enabled)
    return {"state":health.state.value,"process":{"state":"running" if health.process_running else health.state.value,"running":health.process_running,"pid":health.pid,"uptime_seconds":health.uptime_seconds},"discord":{"connected":health.discord_connected,"ready":health.discord_ready,"latency_ms":health.latency_ms,"guild_count":health.guild_count,"last_heartbeat_at":health.last_heartbeat_at,"ready_at":health.ready_at,"last_ready_at":health.last_ready_at,"heartbeat_fresh":health.heartbeat_fresh},"detail":health.detail,"supervisor_available":health.supervisor_available,"instance_id":health.instance_id}
@router.get("/api/bots/{bot_id}/telemetry")
async def bot_telemetry(bot:Bot=Depends(requires_bot_permission("bot.view"))):
    try: return await process_manager.client.telemetry(bot.id)
    except SupervisorUnavailable: return {"available":False,"stale":False,"sample":None}
@router.get("/api/bots/{bot_id}/telemetry/history")
async def bot_telemetry_history(minutes:int=Query(60,ge=1,le=1440),bot:Bot=Depends(requires_bot_permission("bot.view"))):
    window=min(minutes,get_settings().telemetry_history_minutes)
    try: return await process_manager.client.telemetry_history(bot.id,window)
    except SupervisorUnavailable: return {"minutes":window,"samples":[]}
@router.get("/api/supervisor/status")
async def supervisor_status(_:User=Depends(requires_owner)): return await process_manager.supervisor_health()
@router.post("/api/bots/{bot_id}/process/{action}")
async def process_action(request:Request,action:str,csrf_token:str=Form(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    if action not in {"start","stop","restart"}: raise HTTPException(404,"Resource not found")
    bot_id=request.path_params["bot_id"]; bot=PermissionService(db).visible_bot(user,bot_id)
    if not bot or not PermissionService(db).has(user,f"bot.{action}",bot_id): raise HTTPException(404,"Resource not found")
    session=session_from_request(request,db); require_csrf(request,session,csrf_token)
    try: workflow_lock=process_workflow_locks.acquire(bot.id); workflow_lock.__enter__()
    except ProcessConflict as exc: raise HTTPException(409,str(exc)) from exc
    previous=await process_manager.get_status(bot.id,bot.enabled); op=create_operation(db,"activity",user_id=user.id,bot_id=bot.id,event_metadata={"action":action,"before":previous.state.value}); op.status=OperationStatus.RUNNING
    requested=DomainEvent({"start":EventType.BOT_START_REQUESTED,"stop":EventType.BOT_STOP_REQUESTED,"restart":EventType.BOT_RESTART_REQUESTED}[action],user,bot.id,{"before":previous.state.value}); EventBus(db).publish(requested); AuditService(db).record(requested,"requested",bot.id,op.public_id); db.commit()
    try:
        health=await getattr(process_manager,f"{action}_bot")(bot)
    except (ProcessConflict,ProcessLaunchError) as exc:
        op.status=OperationStatus.FAILED; op.completed_at=utcnow(); op.error=str(exc); event=DomainEvent(EventType.BOT_PROCESS_FAILED,user,bot.id,{"action":action,"before":previous.state.value,"error":str(exc)}); EventBus(db).publish(event); AuditService(db).record(event,"failed",bot.id,op.public_id); db.commit(); raise HTTPException(409,str(exc)) from exc
    finally: workflow_lock.__exit__(None,None,None)
    event_type={"start":EventType.BOT_STARTED,"stop":EventType.BOT_STOPPED,"restart":EventType.BOT_RESTARTED}[action]; payload={"before":previous.state.value,"after":health.state.value}; event=DomainEvent(event_type,user,bot.id,payload); EventBus(db).publish(event); AuditService(db).record(event,"success",bot.id,op.public_id); op.status=OperationStatus.COMPLETED; op.completed_at=utcnow(); op.event_metadata={"action":action,**payload}; db.commit()
    return JSONResponse({"operation_id":op.public_id,"state":health.state.value,"detail":health.detail})
