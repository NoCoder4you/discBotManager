import asyncio
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, requires_bot_permission
from app.core.security import require_csrf, session_from_request
from app.database import get_db
from app.models import Backup, BackupType, Bot, PlatformRole, RestorePolicy, User, VerificationStatus
from app.schemas import CreateBackup, PinBackup, RestoreBackup
from app.services.backups import BackupConflict, BackupError, BackupNotRestorable, BackupService
from app.services.permissions import PermissionService
from app.services.process_manager import ProcessConflict, ProcessLaunchError, process_manager

router=APIRouter(tags=["backups"])

def _csrf(request,db,token):
    session=session_from_request(request,db)
    if not session: raise HTTPException(401,"Authentication required")
    require_csrf(request,session,token)

def _backup(db,bot,backup_id):
    row=db.scalar(select(Backup).where(Backup.public_id==backup_id,Backup.bot_id==bot.id))
    if not row: raise HTTPException(404,"Resource not found")
    return row

def _parsed(model,values):
    try: return model.model_validate(values)
    except ValidationError as exc: raise HTTPException(422,exc.errors(include_url=False)) from exc

def _context(request,db,user,bot,**values):
    session=session_from_request(request,db); permissions={key:PermissionService(db).has(user,key,bot.id) for key in ("backups.view","backups.create","backups.restore")}
    return {"user":user,"bot":bot,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token,"permissions":permissions,**values}

@router.get("/bots/{bot_id}/backups",response_class=HTMLResponse)
def backups_page(request:Request,backup_type:str="",verification:str="",created_by:int|None=None,pinned:bool|None=None,day:date|None=None,bot:Bot=Depends(requires_bot_permission("backups.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    stmt=select(Backup).where(Backup.bot_id==bot.id)
    if backup_type:
        try: stmt=stmt.where(Backup.backup_type==BackupType(backup_type))
        except ValueError: raise HTTPException(422,"Invalid backup type")
    if verification:
        try: stmt=stmt.where(Backup.verification_status==VerificationStatus(verification))
        except ValueError: raise HTTPException(422,"Invalid verification status")
    if created_by is not None: stmt=stmt.where(Backup.created_by_id==created_by)
    if pinned is not None: stmt=stmt.where(Backup.pinned==pinned)
    if day: stmt=stmt.where(func.date(Backup.created_at)==day.isoformat())
    rows=list(db.scalars(stmt.order_by(Backup.created_at.desc()).limit(200)))
    return request.app.state.templates.TemplateResponse(request,"backups.html",_context(request,db,user,bot,backups=rows,backup_type=backup_type,verification=verification))

@router.get("/bots/{bot_id}/backups/{backup_id}",response_class=HTMLResponse)
def backup_detail(request:Request,backup_id:str,bot:Bot=Depends(requires_bot_permission("backups.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    backup=_backup(db,bot,backup_id)
    try: preview=BackupService(db).preview(bot,backup)
    except BackupError: preview={"eligible":False,"files":[]}
    return request.app.state.templates.TemplateResponse(request,"backup_detail.html",_context(request,db,user,bot,backup=backup,preview=preview,confirmation=bot.id.upper()))

@router.get("/api/bots/{bot_id}/backups")
def backup_list(bot:Bot=Depends(requires_bot_permission("backups.view")),db:Session=Depends(get_db)):
    rows=db.scalars(select(Backup).where(Backup.bot_id==bot.id).order_by(Backup.created_at.desc()).limit(200))
    return [{"backup_id":x.public_id,"type":x.backup_type.value,"created_at":x.created_at,"size_bytes":x.size_bytes,"file_count":x.file_count,"verification":x.verification_status.value,"pinned":x.pinned,"protected":x.protected,"restore_count":x.restore_count} for x in rows]

@router.get("/api/bots/{bot_id}/backups/{backup_id}/preview")
def backup_preview(backup_id:str,bot:Bot=Depends(requires_bot_permission("backups.view")),db:Session=Depends(get_db)):
    backup=_backup(db,bot,backup_id)
    try: return BackupService(db).preview(bot,backup)
    except BackupError as exc: raise HTTPException(409,"Backup preview is unavailable") from exc

@router.post("/api/bots/{bot_id}/backups")
def create_backup(request:Request,reason:str|None=Form(None),csrf_token:str=Form(...),bot:Bot=Depends(requires_bot_permission("backups.create")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    _csrf(request,db,csrf_token); data=_parsed(CreateBackup,{"reason":reason})
    try: backup=BackupService(db).create(bot,user,reason=data.reason)
    except BackupError as exc: raise HTTPException(409,str(exc)) from exc
    if "text/html" in request.headers.get("accept",""): return RedirectResponse(f"/bots/{bot.id}/backups/{backup.public_id}?operation={backup.operation_id}",303)
    return JSONResponse({"backup_id":backup.public_id,"operation_id":backup.operation_id,"verification":backup.verification_status.value},201)

@router.post("/api/bots/{bot_id}/backups/{backup_id}/pin")
def pin_backup(request:Request,backup_id:str,pinned:bool=Form(...),csrf_token:str=Form(...),bot:Bot=Depends(requires_bot_permission("backups.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    if user.platform_role is not PlatformRole.OWNER: raise HTTPException(404,"Resource not found")
    _csrf(request,db,csrf_token); data=_parsed(PinBackup,{"pinned":pinned}); backup=_backup(db,bot,backup_id); BackupService(db).pin(backup,user,data.pinned)
    return RedirectResponse(f"/bots/{bot.id}/backups/{backup.public_id}",303)

@router.post("/api/bots/{bot_id}/backups/{backup_id}/restore")
async def restore_backup(request:Request,backup_id:str,confirmation:str=Form(...),csrf_token:str=Form(...),bot:Bot=Depends(requires_bot_permission("backups.restore")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    _csrf(request,db,csrf_token); data=_parsed(RestoreBackup,{"confirmation":confirmation})
    if data.confirmation != bot.id.upper(): raise HTTPException(422,"Confirmation did not match the bot identifier")
    backup=_backup(db,bot,backup_id); was_running=False; stopped=False
    try:
        service=BackupService(db)
        safety=await asyncio.to_thread(service.create,bot,user,BackupType.PRE_RESTORE,f"Before restoring {backup.public_id}",True)
        if bot.restore_policy is RestorePolicy.REQUIRES_STOP:
            health=await process_manager.get_status(bot.id,bot.enabled); was_running=health.process_running
            if was_running: await process_manager.stop_bot(bot); stopped=True
        safety,_=await asyncio.to_thread(service.restore,bot,backup,user,None,safety)
        restart={"attempted":False,"ready":None,"detail":None}
        if stopped:
            restart["attempted"]=True; health=await process_manager.start_bot(bot)
            deadline=asyncio.get_running_loop().time()+60
            while not health.discord_ready and asyncio.get_running_loop().time()<deadline:
                await asyncio.sleep(1); health=await process_manager.get_status(bot.id,bot.enabled)
            restart["ready"]=health.discord_ready
            if not health.discord_ready: restart["detail"]="Data restore completed, but the bot did not reach Discord Ready before timeout"
        return {"backup_id":backup.public_id,"safety_backup_id":safety.public_id,"data_restored":True,"restart":restart}
    except (BackupConflict,BackupNotRestorable,ProcessConflict,ProcessLaunchError,BackupError) as exc:
        restart_error=None
        if stopped:
            try: await process_manager.start_bot(bot)
            except Exception as restart_exc: restart_error=str(restart_exc)
        detail=str(exc)
        if restart_error: detail=f"{detail}. Rollback/recovery restart also failed: {restart_error}"
        status=409 if isinstance(exc,(BackupConflict,BackupNotRestorable,ProcessConflict,ProcessLaunchError)) else 500
        raise HTTPException(status,detail) from exc
