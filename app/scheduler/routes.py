from __future__ import annotations
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user, requires_bot_permission, requires_owner
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.core.security import require_csrf, session_from_request
from app.database import get_db
from app.models import Bot, OperationStatus, TaskRun, TaskSchedule, User
from app.scheduler.registry import TaskRegistry
from app.scheduler.schemas import ScheduleUpdate
from app.services.permissions import PermissionService
from app.services.process_manager import ProcessConflict, SupervisorUnavailable, process_manager

router=APIRouter(); registry=TaskRegistry(); _last_runs={}
def _task_or_404(bot,task_id):
    try: task=registry.resolve(bot,task_id)
    except ValueError: task=None
    if not task: raise HTTPException(404,"Resource not found")
    return task
def _csrf(request,db,token):
    session=session_from_request(request,db)
    if not session: raise HTTPException(401,"Authentication required")
    require_csrf(request,session,token)

@router.get("/bots/{bot_id}/scheduler",response_class=HTMLResponse)
def scheduler_page(request:Request,bot:Bot=Depends(requires_bot_permission("scheduler.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    permissions={key:PermissionService(db).has(user,key,bot.id) for key in ("scheduler.run","scheduler.edit")}; session=session_from_request(request,db); schedules={x.task_id:x for x in db.scalars(select(TaskSchedule).where(TaskSchedule.bot_id==bot.id))}; tasks=[]
    for task in registry.tasks_for(bot): tasks.append({"definition":task,"schedule":schedules.get(task.id),"last":db.scalar(select(TaskRun).where(TaskRun.bot_id==bot.id,TaskRun.task_id==task.id).order_by(TaskRun.id.desc()).limit(1))})
    return request.app.state.templates.TemplateResponse(request,"scheduler.html",{"user":user,"bot":bot,"tasks":tasks,"permissions":permissions,"csrf_token":session.csrf_token,"is_owner":user.platform_role.value=="owner"})

@router.get("/api/bots/{bot_id}/tasks")
def task_list(bot:Bot=Depends(requires_bot_permission("scheduler.view")),db:Session=Depends(get_db)):
    schedules={x.task_id:x for x in db.scalars(select(TaskSchedule).where(TaskSchedule.bot_id==bot.id))}
    return {"tasks":[{"id":t.id,"name":t.name,"description":t.description,"category":t.category,"danger":t.danger,"manual_run_allowed":t.manual_run_allowed,"schedule_editable":t.schedule_editable,"schedule":schedule_payload(schedules.get(t.id))} for t in registry.tasks_for(bot)]}

@router.get("/api/bots/{bot_id}/tasks/{task_id}/history")
def task_history(task_id:str,page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),bot:Bot=Depends(requires_bot_permission("scheduler.view")),db:Session=Depends(get_db)):
    _task_or_404(bot,task_id); query=select(TaskRun).where(TaskRun.bot_id==bot.id,TaskRun.task_id==task_id); total=db.scalar(select(func.count()).select_from(query.subquery())) or 0; rows=list(db.scalars(query.order_by(TaskRun.id.desc()).offset((page-1)*page_size).limit(page_size)))
    return {"page":page,"page_size":page_size,"total":total,"runs":[run_payload(x) for x in rows]}

@router.get("/api/bots/{bot_id}/task-runs/{run_id}")
def task_run_detail(run_id:str,bot:Bot=Depends(requires_bot_permission("scheduler.view")),db:Session=Depends(get_db)):
    row=db.scalar(select(TaskRun).where(TaskRun.bot_id==bot.id,TaskRun.public_id==run_id))
    if not row or not registry.resolve(bot,row.task_id): raise HTTPException(404,"Resource not found")
    return run_payload(row)

@router.post("/api/bots/{bot_id}/tasks/{task_id}/run")
async def run_task(request:Request,task_id:str,csrf_token:str=Form(...),confirmation:bool=Form(False),user:User=Depends(current_user),db:Session=Depends(get_db)):
    bot_id=request.path_params["bot_id"]; bot=PermissionService(db).visible_bot(user,bot_id)
    if not bot or not PermissionService(db).has(user,"scheduler.run",bot_id): raise HTTPException(404,"Resource not found")
    task=_task_or_404(bot,task_id); _csrf(request,db,csrf_token)
    if not task.manual_run_allowed: raise HTTPException(409,"This task cannot be run manually")
    if task.danger in {"high","critical"} and not confirmation: raise HTTPException(409,"Confirmation is required")
    key=(user.id,bot.id,task.id); now=time.monotonic()
    if now-_last_runs.get(key,0)<2: raise HTTPException(429,"Please wait before running this task again")
    _last_runs[key]=now; op=create_operation(db,"activity",user_id=user.id,bot_id=bot.id,event_metadata={"task_id":task.id,"trigger":"manual"}); op.status=OperationStatus.RUNNING
    event=DomainEvent(EventType.TASK_RUN_REQUESTED,user,bot.id,{"task_id":task.id,"trigger":"manual","operation_id":op.public_id}); EventBus(db).publish(event); AuditService(db).record(event,"requested",task.id,op.public_id); db.commit()
    try: result=await process_manager.client.run_task(bot.id,task.id,{"trigger":"manual","operation_id":op.public_id,"user_id":user.id,"actor":user.display_name})
    except (ProcessConflict,SupervisorUnavailable) as exc: op.status=OperationStatus.FAILED; op.error=str(exc); db.commit(); raise HTTPException(409,str(exc)) from exc
    return JSONResponse(result,202)

@router.post("/api/bots/{bot_id}/tasks/{task_id}/schedule")
async def update_schedule(request:Request,task_id:str,csrf_token:str=Form(...),enabled:bool=Form(False),timezone_name:str=Form(alias="timezone"),schedule_type:str=Form(...),every:int|None=Form(None),unit:str|None=Form(None),hour:int|None=Form(None),minute:int|None=Form(None),weekday:int|None=Form(None),day:int|None=Form(None),run_at:str|None=Form(None),user:User=Depends(current_user),db:Session=Depends(get_db)):
    bot_id=request.path_params["bot_id"]; bot=PermissionService(db).visible_bot(user,bot_id)
    if not bot or not PermissionService(db).has(user,"scheduler.edit",bot_id): raise HTTPException(404,"Resource not found")
    task=_task_or_404(bot,task_id); _csrf(request,db,csrf_token); config={"type":schedule_type}
    if schedule_type=="interval": config.update(every=every,unit=unit)
    elif schedule_type in {"daily","weekly","monthly"}: config.update(hour=hour,minute=minute)
    if schedule_type=="weekly": config["weekday"]=weekday
    if schedule_type=="monthly": config["day"]=day
    if schedule_type=="one_time": config["run_at"]=run_at
    try: payload=ScheduleUpdate.model_validate({"enabled":enabled,"timezone":timezone_name,"config":config})
    except ValidationError as exc: raise HTTPException(422,"Invalid structured schedule") from exc
    before=db.scalar(select(TaskSchedule).where(TaskSchedule.bot_id==bot.id,TaskSchedule.task_id==task.id)); prior=schedule_payload(before); op=create_operation(db,"activity",user_id=user.id,bot_id=bot.id,event_metadata={"task_id":task.id,"before":prior}); op.status=OperationStatus.RUNNING; db.commit()
    try: result=await process_manager.client.configure_schedule(bot.id,task.id,payload.model_dump(mode="json"))
    except (ProcessConflict,SupervisorUnavailable) as exc: op.status=OperationStatus.FAILED; op.error=str(exc); db.commit(); raise HTTPException(409,str(exc)) from exc
    db.expire_all(); event_type=EventType.TASK_SCHEDULE_ENABLED if result["enabled"] and not (before and before.enabled) else EventType.TASK_SCHEDULE_DISABLED if not result["enabled"] and before and before.enabled else EventType.TASK_SCHEDULE_CHANGED
    event=DomainEvent(event_type,user,bot.id,{"task_id":task.id,"operation_id":op.public_id,"before":prior,"after":result}); EventBus(db).publish(event); AuditService(db).record(event,"success",task.id,op.public_id); op.status=OperationStatus.COMPLETED; op.completed_at=datetime.now(timezone.utc); db.commit(); return RedirectResponse(f"/bots/{bot.id}/scheduler?operation={op.public_id}",303)

@router.post("/api/bots/{bot_id}/tasks/{task_id}/toggle")
async def toggle_schedule(request:Request,task_id:str,csrf_token:str=Form(...),enabled:bool=Form(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    bot_id=request.path_params["bot_id"]; bot=PermissionService(db).visible_bot(user,bot_id)
    if not bot or not PermissionService(db).has(user,"scheduler.edit",bot_id): raise HTTPException(404,"Resource not found")
    task=_task_or_404(bot,task_id); _csrf(request,db,csrf_token); before=schedule_payload(db.scalar(select(TaskSchedule).where(TaskSchedule.bot_id==bot.id,TaskSchedule.task_id==task.id)))
    op=create_operation(db,"activity",user_id=user.id,bot_id=bot.id,event_metadata={"task_id":task.id,"before":before}); op.status=OperationStatus.RUNNING; db.commit()
    try: result=await process_manager.client.toggle_schedule(bot.id,task.id,enabled)
    except (ProcessConflict,SupervisorUnavailable) as exc: op.status=OperationStatus.FAILED; op.error=str(exc); op.completed_at=datetime.now(timezone.utc); db.commit(); raise HTTPException(409,str(exc)) from exc
    event=DomainEvent(EventType.TASK_SCHEDULE_ENABLED if enabled else EventType.TASK_SCHEDULE_DISABLED,user,bot.id,{"task_id":task.id,"operation_id":op.public_id,"before":before,"after":result}); EventBus(db).publish(event); AuditService(db).record(event,"success",task.id,op.public_id); op.status=OperationStatus.COMPLETED; op.completed_at=datetime.now(timezone.utc); db.commit(); return RedirectResponse(f"/bots/{bot.id}/scheduler?operation={op.public_id}",303)

@router.get("/api/scheduler/health")
async def scheduler_health(_:User=Depends(requires_owner)):
    try: return await process_manager.client.scheduler_health()
    except SupervisorUnavailable: return JSONResponse({"status":"unavailable","available":False},503)

def schedule_payload(row):
    if not row:return None
    return {"enabled":row.enabled,"type":row.schedule_type,"config":row.structured_config,"timezone":row.timezone,"next_run_at":row.next_run_at.isoformat() if row.next_run_at else None,"last_run_at":row.last_run_at.isoformat() if row.last_run_at else None,"last_status":row.last_status,"status":"orphaned" if row.reconciliation_required else "available"}
def run_payload(row):
    return {"run_id":row.public_id,"task_id":row.task_id,"trigger":row.trigger,"status":row.status,"actor":row.actor_display,"operation_id":row.operation_id,"queued_at":row.queued_at.isoformat(),"started_at":row.started_at.isoformat() if row.started_at else None,"finished_at":row.finished_at.isoformat() if row.finished_at else None,"duration_ms":row.duration_ms,"summary":row.summary,"details":row.result_metadata}
