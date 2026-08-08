from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user, requires_bot_permission, requires_owner
from app.database import get_db
from app.models import Bot, PlatformRole, User
from app.services.permissions import PermissionService
from app.core.security import session_from_request
from app.core.security import require_csrf
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.models import OperationStatus, utcnow
from app.services.process_manager import ProcessConflict, ProcessLaunchError, process_manager
router=APIRouter()
@router.get("/",response_class=HTMLResponse)
def home(request:Request): return request.app.state.templates.TemplateResponse(request,"login.html",{})
@router.get("/dashboard",response_class=HTMLResponse)
def dashboard(request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    bots=PermissionService(db).visible_bots(user); session=session_from_request(request,db); return request.app.state.templates.TemplateResponse(request,"dashboard.html",{"user":user,"bots":bots,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token})
@router.get("/bots/{bot_id}",response_class=HTMLResponse)
def bot_detail(request:Request,bot:Bot=Depends(requires_bot_permission("bot.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    session=session_from_request(request,db); permissions={key:PermissionService(db).has(user,key,bot.id) for key in ("bot.start","bot.stop","bot.restart")}; return request.app.state.templates.TemplateResponse(request,"bot.html",{"user":user,"bot":bot,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token,"permissions":permissions})
@router.get("/api/bots/{bot_id}/status")
async def bot_status(bot:Bot=Depends(requires_bot_permission("bot.view"))):
    health=await process_manager.get_status(bot.id,bot.enabled); return {"state":health.state.value,"process_running":health.process_running,"discord_connected":health.discord_connected,"discord_ready":health.discord_ready,"detail":health.detail,"pid":health.pid,"instance_id":health.instance_id,"uptime_seconds":health.uptime_seconds,"supervisor_available":health.supervisor_available}
@router.get("/api/supervisor/status")
async def supervisor_status(_:User=Depends(requires_owner)): return await process_manager.supervisor_health()
@router.post("/api/bots/{bot_id}/process/{action}")
async def process_action(request:Request,action:str,csrf_token:str=Form(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    if action not in {"start","stop","restart"}: raise HTTPException(404,"Resource not found")
    bot_id=request.path_params["bot_id"]; bot=PermissionService(db).visible_bot(user,bot_id)
    if not bot or not PermissionService(db).has(user,f"bot.{action}",bot_id): raise HTTPException(404,"Resource not found")
    session=session_from_request(request,db); require_csrf(request,session,csrf_token)
    previous=await process_manager.get_status(bot.id,bot.enabled); op=create_operation(db,"activity",user_id=user.id,bot_id=bot.id,event_metadata={"action":action,"before":previous.state.value}); op.status=OperationStatus.RUNNING
    requested=DomainEvent({"start":EventType.BOT_START_REQUESTED,"stop":EventType.BOT_STOP_REQUESTED,"restart":EventType.BOT_RESTART_REQUESTED}[action],user,bot.id,{"before":previous.state.value}); EventBus(db).publish(requested); AuditService(db).record(requested,"requested",bot.id,op.public_id); db.commit()
    try:
        health=await getattr(process_manager,f"{action}_bot")(bot)
    except (ProcessConflict,ProcessLaunchError) as exc:
        op.status=OperationStatus.FAILED; op.completed_at=utcnow(); op.error=str(exc); event=DomainEvent(EventType.BOT_PROCESS_FAILED,user,bot.id,{"action":action,"before":previous.state.value,"error":str(exc)}); EventBus(db).publish(event); AuditService(db).record(event,"failed",bot.id,op.public_id); db.commit(); raise HTTPException(409,str(exc)) from exc
    event_type={"start":EventType.BOT_STARTED,"stop":EventType.BOT_STOPPED,"restart":EventType.BOT_RESTARTED}[action]; payload={"before":previous.state.value,"after":health.state.value}; event=DomainEvent(event_type,user,bot.id,payload); EventBus(db).publish(event); AuditService(db).record(event,"success",bot.id,op.public_id); op.status=OperationStatus.COMPLETED; op.completed_at=utcnow(); op.event_metadata={"action":action,**payload}; db.commit()
    return JSONResponse({"operation_id":op.public_id,"state":health.state.value,"detail":health.detail})
