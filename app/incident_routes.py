from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user
from app.core.security import session_from_request
from app.database import get_db
from app.models import Bot, ErrorGroup, Incident, IncidentEvent, IncidentSeverity, IncidentStatus, PlatformRole, User
from app.services.permissions import PermissionService

router=APIRouter()
PAGE_SIZE=20
def _scope(db,user):
    if user.platform_role is PlatformRole.OWNER: return None
    permissions=PermissionService(db)
    return [bot.id for bot in permissions.visible_bots(user) if permissions.has(user,"errors.view",bot.id)]
def _allowed(db,user,incident):
    return bool(incident and (user.platform_role is PlatformRole.OWNER or (incident.bot_id and PermissionService(db).has(user,"errors.view",incident.bot_id))))
@router.get("/incidents",response_class=HTMLResponse)
def incidents(request:Request,page:int=Query(1,ge=1),bot_id:str|None=None,severity:str|None=None,status:str|None=None,incident_type:str|None=None,q:str|None=Query(None,max_length=100),user:User=Depends(current_user),db:Session=Depends(get_db)):
    scope=_scope(db,user); query=select(Incident)
    if scope is not None: query=query.where(Incident.bot_id.in_(scope))
    if bot_id:
        if scope is not None and bot_id not in scope: raise HTTPException(404,"Resource not found")
        query=query.where(Incident.bot_id==bot_id)
    if severity in IncidentSeverity.__members__: query=query.where(Incident.severity==IncidentSeverity[severity])
    if status in IncidentStatus.__members__: query=query.where(Incident.status==IncidentStatus[status])
    if incident_type: query=query.where(Incident.incident_type==incident_type)
    if q: query=query.where(or_(Incident.public_id.ilike(f"%{q}%"),Incident.title.ilike(f"%{q}%"),Incident.summary.ilike(f"%{q}%")))
    total=db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows=list(db.scalars(query.order_by(Incident.started_at.desc(),Incident.id.desc()).offset((page-1)*PAGE_SIZE).limit(PAGE_SIZE)))
    group_query=select(ErrorGroup)
    if scope is not None: group_query=group_query.where(ErrorGroup.bot_id.in_(scope))
    groups=list(db.scalars(group_query.order_by(ErrorGroup.last_seen.desc(),ErrorGroup.id.desc()).limit(10)))
    bots={b.id:b for b in db.scalars(select(Bot).where(Bot.id.in_({x.bot_id for x in rows if x.bot_id})))}
    counts={severity.value:db.scalar(select(func.count(Incident.id)).where(Incident.status==IncidentStatus.OPEN,Incident.severity==severity,*([] if scope is None else [Incident.bot_id.in_(scope)]))) or 0 for severity in IncidentSeverity}
    session=session_from_request(request,db)
    return request.app.state.templates.TemplateResponse(request,"incidents.html",{"user":user,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token,"incidents":rows,"bots":bots,"counts":counts,"groups":groups,"total":total,"page":page,"pages":max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)})
@router.get("/incidents/{incident_id}",response_class=HTMLResponse)
def incident_detail(request:Request,incident_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    row=db.scalar(select(Incident).where(Incident.public_id==incident_id))
    if not _allowed(db,user,row): raise HTTPException(404,"Resource not found")
    permissions=PermissionService(db); bot=db.get(Bot,row.bot_id) if row.bot_id else None
    context=dict(row.context or {}); console_present="console_excerpt" in context
    if not (row.bot_id and permissions.has(user,"console.view",row.bot_id)): context.pop("console_excerpt",None)
    for permission,keys in (("backups.view",("backup_id",)),("scheduler.view",("task_id","task_name","run_id","trigger","duration_ms")),("database.view",("database_id","table"))):
        if row.bot_id and not permissions.has(user,permission,row.bot_id):
            for key in keys: context.pop(key,None)
    timeline=list(db.scalars(select(IncidentEvent).where(IncidentEvent.incident_id==row.id).order_by(IncidentEvent.timestamp,IncidentEvent.id)))
    session=session_from_request(request,db)
    return request.app.state.templates.TemplateResponse(request,"incident_detail.html",{"user":user,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token,"incident":row,"bot":bot,"context":context,"console_present":console_present,"can_console":bool(row.bot_id and permissions.has(user,"console.view",row.bot_id)),"timeline":timeline})
@router.get("/errors/{group_id}",response_class=HTMLResponse)
def error_detail(request:Request,group_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    group=db.scalar(select(ErrorGroup).where(ErrorGroup.public_id==group_id))
    if not group or (user.platform_role is not PlatformRole.OWNER and not PermissionService(db).has(user,"errors.view",group.bot_id)): raise HTTPException(404,"Resource not found")
    session=session_from_request(request,db); bot=db.get(Bot,group.bot_id); latest=db.get(Incident,group.latest_incident_id) if group.latest_incident_id else None
    return request.app.state.templates.TemplateResponse(request,"error_group.html",{"user":user,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token,"group":group,"bot":bot,"latest":latest})
