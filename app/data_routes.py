from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user, requires_bot_permission
from app.core.security import require_csrf, session_from_request
from app.database import get_db
from app.models import Bot, DataVersion, User
from app.schemas import ConfigSave, JsonSave
from app.services.data import BotDataService, DataConflict, DataError, DataNotFound, DataValidationError
from app.services.permissions import PermissionService
router=APIRouter()
def failure(exc):
    if isinstance(exc,DataNotFound): return HTTPException(404,"Resource not found")
    if isinstance(exc,DataConflict): return HTTPException(409,str(exc))
    if isinstance(exc,DataValidationError): return HTTPException(422,{"message":str(exc),"errors":exc.errors})
    return HTTPException(400,str(exc))
def csrf(request,db,token):
    row=session_from_request(request,db)
    if not row: raise HTTPException(401,"Authentication required")
    require_csrf(request,row,token)
def context(request,db,user,bot,**values):
    row=session_from_request(request,db); return {"user":user,"bot":bot,"is_owner":user.platform_role.value=="owner","csrf_token":row.csrf_token,**values}
@router.get("/bots/{bot_id}/data",response_class=HTMLResponse)
def data_page(request:Request,bot:Bot=Depends(requires_bot_permission("files.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    service=BotDataService(db)
    try: entries=service.list_directory(bot); sources=service.sources(bot)
    except DataError as exc: raise failure(exc)
    return request.app.state.templates.TemplateResponse(request,"data.html",context(request,db,user,bot,entries=entries,sources=sources,can_edit=PermissionService(db).has(user,"files.edit",bot.id)))
@router.get("/api/bots/{bot_id}/files")
def files(path:str=".",bot:Bot=Depends(requires_bot_permission("files.view")),db:Session=Depends(get_db)):
    try: return {"entries":BotDataService(db).list_directory(bot,path)}
    except DataError as exc: raise failure(exc)
@router.get("/api/bots/{bot_id}/files/{path:path}")
def view_file(path:str,bot:Bot=Depends(requires_bot_permission("files.view")),db:Session=Depends(get_db)):
    try: return BotDataService(db).read(bot,path)
    except DataError as exc: raise failure(exc)
@router.get("/bots/{bot_id}/data/{source_id}",response_class=HTMLResponse)
def editor(source_id:str,request:Request,bot:Bot=Depends(requires_bot_permission("files.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    service=BotDataService(db)
    try: source=service.source(bot,source_id); data=service.read(bot,source.path)
    except DataError as exc: raise failure(exc)
    if source.sensitive_fields: data["content"]=service._redacted_json(data["content"],source.sensitive_fields)
    return request.app.state.templates.TemplateResponse(request,"json_editor.html",context(request,db,user,bot,source=source,data=data,can_edit=source.editable and not source.sensitive_fields and PermissionService(db).has(user,"files.edit",bot.id)))
@router.post("/api/bots/{bot_id}/data/{source_id}/validate")
def validate_json(source_id:str,payload:JsonSave,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("files.edit")),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token); service=BotDataService(db)
    try: source=service.source(bot,source_id); service.validate_json(payload.content,source); return {"valid":True,"validation":"schema" if source.validator else "syntax"}
    except DataError as exc: raise failure(exc)
@router.post("/api/bots/{bot_id}/data/{source_id}")
def save_json(source_id:str,payload:JsonSave,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("files.edit")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token); service=BotDataService(db)
    try: return service.save_json(bot,service.source(bot,source_id),user,payload.content,payload.base_version)
    except DataError as exc: raise failure(exc)
@router.get("/api/bots/{bot_id}/data/{source_id}/history")
def history(source_id:str,bot:Bot=Depends(requires_bot_permission("files.view")),db:Session=Depends(get_db)):
    service=BotDataService(db)
    try: rows=service.history(bot,service.source(bot,source_id))
    except DataError as exc: raise failure(exc)
    return {"versions":[{"id":x.id,"created_at":x.created_at,"actor":x.actor.display_name if x.actor else "System","operation_id":x.operation_id,"previous_hash":x.previous_hash,"new_hash":x.new_hash} for x in rows]}
@router.get("/api/bots/{bot_id}/data/{source_id}/diff/{version_id}")
def version_diff(source_id:str,version_id:int,bot:Bot=Depends(requires_bot_permission("files.view")),db:Session=Depends(get_db)):
    service=BotDataService(db)
    try:
        source=service.source(bot,source_id); version=db.get(DataVersion,version_id)
        if not version: raise DataNotFound("Resource not found")
        return {"diff":service.diff(bot,source,version)}
    except DataError as exc: raise failure(exc)
@router.get("/bots/{bot_id}/configuration",response_class=HTMLResponse)
def config_page(request:Request,bot:Bot=Depends(requires_bot_permission("config.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    service=BotDataService(db)
    return request.app.state.templates.TemplateResponse(request,"configuration.html",context(request,db,user,bot,sources=service.sources(bot,True),can_edit=PermissionService(db).has(user,"config.edit",bot.id)))
@router.get("/api/bots/{bot_id}/configuration/{source_id}")
def config_get(source_id:str,bot:Bot=Depends(requires_bot_permission("config.view")),db:Session=Depends(get_db)):
    service=BotDataService(db)
    try:
        source=service.source(bot,source_id,True)
        fields=[{"key":f.key,"label":f.label,"description":f.description,"type":f.type,"required":f.required,"editable":f.editable,"sensitive":f.sensitive,"choices":f.choices,"minimum":f.minimum,"maximum":f.maximum,"step":f.step,"requires_restart":f.requires_restart} for f in source.config_fields]
        return {**service.config_view(bot,source),"fields":fields}
    except DataError as exc: raise failure(exc)
@router.post("/api/bots/{bot_id}/configuration/{source_id}")
def config_save(source_id:str,payload:ConfigSave,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("config.edit")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token); service=BotDataService(db)
    try: return service.save_config(bot,service.source(bot,source_id,True),user,payload.values,payload.base_version)
    except DataError as exc: raise failure(exc)
