from datetime import date
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from app.auth.dependencies import requires_owner
from app.core.security import require_csrf, session_from_request
from app.database import get_db
from app.models import AuditLog, Bot, BotAssignment, Permission, Role, User, UserPermission
from app.schemas import AssignmentMutation, BotMutation, PermissionOverrideMutation, UserStatusUpdate
from app.services.admin import AdminError, AdminService
from app.services.console_stream import console_subscriptions

router=APIRouter(prefix="/admin",tags=["administration"])
def page_context(request,db,user,**values):
    session=session_from_request(request,db); return {"user":user,"is_owner":True,"csrf_token":session.csrf_token,**values}
def validate_csrf(request,db,token):
    session=session_from_request(request,db)
    if not session: raise HTTPException(401,"Authentication required")
    require_csrf(request,session,token)
def parsed(model,values):
    try: return model.model_validate(values)
    except ValidationError as exc: raise HTTPException(422,exc.errors(include_url=False)) from exc
def redirect(path,operation): return RedirectResponse(f"{path}?operation={operation.public_id}",303)

@router.get("/users",response_class=HTMLResponse)
def users(request:Request,q:str=Query("",max_length=100),page:int=Query(1,ge=1),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    stmt=select(User)
    if q: stmt=stmt.where(or_(User.username.ilike(f"%{q}%"),User.display_name.ilike(f"%{q}%"),User.discord_id==q))
    rows=list(db.scalars(stmt.order_by(User.created_at.desc()).offset((page-1)*25).limit(25)))
    counts=dict(db.execute(select(BotAssignment.user_id,func.count()).group_by(BotAssignment.user_id)).all())
    return request.app.state.templates.TemplateResponse(request,"admin_users.html",page_context(request,db,owner,users=rows,counts=counts,q=q,page=page))
@router.get("/users/{user_id}",response_class=HTMLResponse)
def user_detail(user_id:int,request:Request,owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    target=db.get(User,user_id)
    if not target: raise HTTPException(404,"Resource not found")
    assignments=list(db.scalars(select(BotAssignment).where(BotAssignment.user_id==user_id)))
    bots=list(db.scalars(select(Bot).order_by(Bot.display_name))); roles=list(db.scalars(select(Role).where(Role.scope=="bot"))); permissions=list(db.scalars(select(Permission).order_by(Permission.key)))
    overrides=list(db.scalars(select(UserPermission).where(UserPermission.user_id==user_id)))
    audits=list(db.scalars(select(AuditLog).where(AuditLog.target==target.discord_id).order_by(AuditLog.timestamp.desc()).limit(20)))
    return request.app.state.templates.TemplateResponse(request,"admin_user.html",page_context(request,db,owner,target=target,assignments=assignments,bots=bots,roles=roles,permissions=permissions,overrides=overrides,audits=audits))
@router.post("/users/{user_id}/status")
def user_status(user_id:int,request:Request,enabled:bool=Form(...),csrf_token:str=Form(...),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    validate_csrf(request,db,csrf_token); data=parsed(UserStatusUpdate,{"enabled":enabled}); target=db.get(User,user_id)
    if not target: raise HTTPException(404,"Resource not found")
    try: op=AdminService(db).set_user_enabled(owner,target,data.enabled)
    except AdminError as exc: raise HTTPException(409,str(exc)) from exc
    if not data.enabled: console_subscriptions.revoke(target.id)
    return redirect(f"/admin/users/{user_id}",op)
@router.post("/users/{user_id}/assignments")
def assignment(user_id:int,request:Request,bot_id:str=Form(...),role_key:str=Form(...),enabled:bool=Form(False),csrf_token:str=Form(...),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    validate_csrf(request,db,csrf_token); data=parsed(AssignmentMutation,{"bot_id":bot_id,"role_key":role_key,"enabled":enabled}); target=db.get(User,user_id)
    if not target: raise HTTPException(404,"Resource not found")
    try: _,op=AdminService(db).assign(owner,target,data)
    except AdminError as exc: raise HTTPException(409,str(exc)) from exc
    console_subscriptions.revoke(target.id,data.bot_id)
    return redirect(f"/admin/users/{user_id}",op)
@router.post("/users/{user_id}/assignments/{bot_id}/revoke")
def revoke(user_id:int,bot_id:str,request:Request,csrf_token:str=Form(...),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    validate_csrf(request,db,csrf_token); target=db.get(User,user_id)
    if not target: raise HTTPException(404,"Resource not found")
    try: op=AdminService(db).revoke(owner,target,bot_id)
    except AdminError as exc: raise HTTPException(409,str(exc)) from exc
    console_subscriptions.revoke(target.id,bot_id)
    return redirect(f"/admin/users/{user_id}",op)
@router.post("/users/{user_id}/assignments/{bot_id}/override")
def override(user_id:int,bot_id:str,request:Request,permission_key:str=Form(...),state:str=Form(...),csrf_token:str=Form(...),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    validate_csrf(request,db,csrf_token); data=parsed(PermissionOverrideMutation,{"permission_key":permission_key,"state":state}); target=db.get(User,user_id)
    if not target: raise HTTPException(404,"Resource not found")
    try: op=AdminService(db).override(owner,target,bot_id,data)
    except AdminError as exc: raise HTTPException(409,str(exc)) from exc
    console_subscriptions.revoke(target.id,bot_id)
    return redirect(f"/admin/users/{user_id}",op)

@router.get("/bots",response_class=HTMLResponse)
def bots(request:Request,q:str=Query("",max_length=100),page:int=Query(1,ge=1),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    stmt=select(Bot)
    if q: stmt=stmt.where(or_(Bot.id.ilike(f"%{q}%"),Bot.display_name.ilike(f"%{q}%")))
    rows=list(db.scalars(stmt.order_by(Bot.display_name).offset((page-1)*25).limit(25)))
    return request.app.state.templates.TemplateResponse(request,"admin_bots.html",page_context(request,db,owner,bots=rows,q=q,page=page))
@router.post("/bots")
def create_bot(request:Request,id:str=Form(...),display_name:str=Form(...),description:str=Form(""),folder:str=Form(...),entry_file:str=Form(...),python_executable:str=Form(...),accent_colour:str=Form("#5865f2"),enabled:bool=Form(False),adapter:str=Form("python"),data_root:str=Form("."),backup_include:str=Form("**/*"),backup_exclude:str=Form(""),restore_policy:str=Form("requires_stop"),csrf_token:str=Form(...),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    validate_csrf(request,db,csrf_token); data=parsed(BotMutation,locals())
    try: _,op=AdminService(db).create_bot(owner,data)
    except AdminError as exc: raise HTTPException(422,str(exc)) from exc
    return redirect("/admin/bots",op)
@router.post("/bots/{bot_id}")
def update_bot(bot_id:str,request:Request,id:str=Form(...),display_name:str=Form(...),description:str=Form(""),folder:str=Form(...),entry_file:str=Form(...),python_executable:str=Form(...),accent_colour:str=Form("#5865f2"),enabled:bool=Form(False),adapter:str=Form("python"),data_root:str=Form("."),backup_include:str=Form("**/*"),backup_exclude:str=Form(""),restore_policy:str=Form("requires_stop"),csrf_token:str=Form(...),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    validate_csrf(request,db,csrf_token); bot=db.get(Bot,bot_id)
    if not bot: raise HTTPException(404,"Resource not found")
    data=parsed(BotMutation,locals())
    try: _,op=AdminService(db).update_bot(owner,bot,data)
    except AdminError as exc: raise HTTPException(422,str(exc)) from exc
    return redirect(f"/bots/{bot.id}",op)

@router.get("/audit",response_class=HTMLResponse)
def audit(request:Request,q:str=Query("",max_length=100),action:str=Query("",max_length=100),result:str=Query("",max_length=30),bot_id:str=Query("",max_length=36),day:date|None=None,page:int=Query(1,ge=1),owner:User=Depends(requires_owner),db:Session=Depends(get_db)):
    stmt=select(AuditLog)
    if q: stmt=stmt.where(or_(AuditLog.operation_id.ilike(f"%{q}%"),AuditLog.target.ilike(f"%{q}%"),AuditLog.discord_user_id==q))
    if action: stmt=stmt.where(AuditLog.action==action)
    if result: stmt=stmt.where(AuditLog.result==result)
    if bot_id: stmt=stmt.where(AuditLog.bot_id==bot_id)
    if day: stmt=stmt.where(func.date(AuditLog.timestamp)==day.isoformat())
    rows=list(db.scalars(stmt.order_by(AuditLog.timestamp.desc()).offset((page-1)*50).limit(50)))
    return request.app.state.templates.TemplateResponse(request,"admin_audit.html",page_context(request,db,owner,records=rows,q=q,page=page))
