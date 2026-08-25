from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user, requires_bot_permission
from app.core.security import require_csrf, session_from_request
from app.database import get_db
from app.models import Bot, User
from app.schemas import DisableMaintenanceRequest, EnableMaintenanceRequest
from app.services.maintenance import MaintenanceService
from app.services.process_manager import ProcessConflict, process_manager, process_workflow_locks
from app.services.permissions import PermissionService

router=APIRouter()

@router.get("/api/bots/{bot_id}/maintenance")
async def status(bot:Bot=Depends(requires_bot_permission("bot.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    row=MaintenanceService(db).get(bot.id)
    health=await process_manager.get_status(bot.id,bot.enabled)
    result=MaintenanceService(db).payload(row,health.process_running)
    if not (PermissionService(db).has(user,"bot.maintenance.enable",bot.id) or PermissionService(db).has(user,"bot.maintenance.disable",bot.id)):
        result.pop("bypass_user_ids",None); result.pop("bypass_roles",None)
    return result

def csrf(request,db,token): require_csrf(request,session_from_request(request,db),token)

@router.post("/api/bots/{bot_id}/maintenance/enable")
async def enable(payload:EnableMaintenanceRequest,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("bot.maintenance.enable")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token)
    try:
        with process_workflow_locks.acquire(bot.id): row,operation=MaintenanceService(db).set(bot,user,True,payload.reason,payload.public_message,payload.planned_end_at)
    except ProcessConflict as exc: raise HTTPException(409,str(exc)) from exc
    health=await process_manager.get_status(bot.id,bot.enabled)
    return {**MaintenanceService(db).payload(row,health.process_running),"operation_id":operation.public_id if operation else None,"changed":operation is not None}

@router.post("/api/bots/{bot_id}/maintenance/disable")
async def disable(payload:DisableMaintenanceRequest,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("bot.maintenance.disable")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token)
    try:
        with process_workflow_locks.acquire(bot.id): row,operation=MaintenanceService(db).set(bot,user,False)
    except ProcessConflict as exc: raise HTTPException(409,str(exc)) from exc
    health=await process_manager.get_status(bot.id,bot.enabled)
    return {**MaintenanceService(db).payload(row,health.process_running),"operation_id":operation.public_id if operation else None,"changed":operation is not None}
