import json
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, requires_bot_permission
from app.core.security import require_csrf, session_from_request
from app.database import get_db
from app.models import Bot, User
from app.schemas import DatabaseCreate, DatabaseDelete, DatabaseFilter, DatabaseUpdate
from app.services.permissions import PermissionService
from app.services.sqlite_data import SQLiteBusy, SQLiteConflict, SQLiteDataError, SQLiteDataService, SQLiteIntegrityError, SQLiteNotFound, SQLiteValidationError
from app.adapters.base import DatabaseEditPolicy

router=APIRouter()
def failure(exc):
    detail=getattr(exc,"workflow_result",None) or str(exc)
    if isinstance(exc,SQLiteNotFound): return HTTPException(404,"Resource not found")
    if isinstance(exc,(SQLiteConflict,SQLiteBusy)): return HTTPException(409,detail)
    if isinstance(exc,(SQLiteValidationError,SQLiteIntegrityError)): return HTTPException(422,detail)
    return HTTPException(400,detail)
def csrf(request,db,token):
    row=session_from_request(request,db)
    if not row: raise HTTPException(401,"Authentication required")
    require_csrf(request,row,token)
def context(request,db,user,bot,**values):
    row=session_from_request(request,db); return {"user":user,"bot":bot,"is_owner":user.platform_role.value=="owner","csrf_token":row.csrf_token,**values}

@router.get("/bots/{bot_id}/databases",response_class=HTMLResponse)
def databases_page(request:Request,bot:Bot=Depends(requires_bot_permission("database.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    service=SQLiteDataService(db); sources=service.overview(bot)
    return request.app.state.templates.TemplateResponse(request,"databases.html",context(request,db,user,bot,sources=sources,can_backup=PermissionService(db).has(user,"backups.create",bot.id)))
@router.get("/bots/{bot_id}/databases/{database_id}",response_class=HTMLResponse)
def database_page(database_id:str,request:Request,bot:Bot=Depends(requires_bot_permission("database.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    service=SQLiteDataService(db)
    try: source,path=service.source(bot,database_id); tables=service.tables(bot,database_id); health=service.health(path)
    except SQLiteDataError as exc: raise failure(exc)
    return request.app.state.templates.TemplateResponse(request,"database.html",context(request,db,user,bot,source=source,tables=tables,health=health))
@router.get("/bots/{bot_id}/databases/{database_id}/{table_name}",response_class=HTMLResponse)
def table_page(database_id:str,table_name:str,request:Request,bot:Bot=Depends(requires_bot_permission("database.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    service=SQLiteDataService(db)
    try: source,_=service.source(bot,database_id); schema=service.schema(bot,database_id,table_name); policy=next(x for x in service.tables(bot,database_id) if x["name"]==table_name)
    except SQLiteDataError as exc: raise failure(exc)
    return request.app.state.templates.TemplateResponse(request,"database_table.html",context(request,db,user,bot,database_id=database_id,table_name=table_name,schema=schema,policy=policy,offline_edit=source.mutation_policy is DatabaseEditPolicy.EDIT_REQUIRES_BOT_STOP,can_edit=PermissionService(db).has(user,"database.edit",bot.id)))
@router.get("/api/bots/{bot_id}/databases")
def database_list(bot:Bot=Depends(requires_bot_permission("database.view")),db:Session=Depends(get_db)): return {"databases":SQLiteDataService(db).overview(bot)}
@router.get("/api/bots/{bot_id}/databases/{database_id}/tables")
def table_list(database_id:str,bot:Bot=Depends(requires_bot_permission("database.view")),db:Session=Depends(get_db)):
    try: return {"tables":SQLiteDataService(db).tables(bot,database_id)}
    except SQLiteDataError as exc: raise failure(exc)
@router.get("/api/bots/{bot_id}/databases/{database_id}/tables/{table_name}/schema")
def table_schema(database_id:str,table_name:str,bot:Bot=Depends(requires_bot_permission("database.view")),db:Session=Depends(get_db)):
    try: return {"columns":SQLiteDataService(db).schema(bot,database_id,table_name)}
    except SQLiteDataError as exc: raise failure(exc)
@router.get("/api/bots/{bot_id}/databases/{database_id}/tables/{table_name}/rows")
def rows(database_id:str,table_name:str,page:int=Query(1,ge=1),page_size:int=Query(50),sort:str|None=None,direction:str="asc",search:str|None=Query(None,max_length=200),filters:str|None=Query(None,max_length=4000),bot:Bot=Depends(requires_bot_permission("database.view")),db:Session=Depends(get_db)):
    try:
        parsed=[] if not filters else [DatabaseFilter.model_validate(x).model_dump() for x in json.loads(filters)]
        return SQLiteDataService(db).browse(bot,database_id,table_name,page,page_size,sort,direction,search,parsed)
    except (ValueError,TypeError,json.JSONDecodeError): raise HTTPException(422,"Invalid structured filters")
    except SQLiteDataError as exc: raise failure(exc)
@router.get("/api/bots/{bot_id}/databases/{database_id}/tables/{table_name}/row")
def row(database_id:str,table_name:str,key:str=Query(...,max_length=2000),bot:Bot=Depends(requires_bot_permission("database.view")),db:Session=Depends(get_db)):
    try: return SQLiteDataService(db).row(bot,database_id,table_name,json.loads(key))
    except (ValueError,TypeError,json.JSONDecodeError): raise HTTPException(422,"Invalid record key")
    except SQLiteDataError as exc: raise failure(exc)
@router.post("/api/bots/{bot_id}/databases/{database_id}/tables/{table_name}/rows")
async def create_row(database_id:str,table_name:str,payload:DatabaseCreate,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("database.edit")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token)
    try: return await SQLiteDataService(db).mutate_process_aware(bot,database_id,table_name,user,"create",values=payload.values)
    except SQLiteDataError as exc: raise failure(exc)
@router.patch("/api/bots/{bot_id}/databases/{database_id}/tables/{table_name}/row")
async def update_row(database_id:str,table_name:str,payload:DatabaseUpdate,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("database.edit")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token)
    try: return await SQLiteDataService(db).mutate_process_aware(bot,database_id,table_name,user,"update",payload.values,payload.key,payload.concurrency_token)
    except SQLiteDataError as exc: raise failure(exc)
@router.delete("/api/bots/{bot_id}/databases/{database_id}/tables/{table_name}/row")
async def delete_row(database_id:str,table_name:str,payload:DatabaseDelete,request:Request,x_csrf_token:str=Header(...),bot:Bot=Depends(requires_bot_permission("database.edit")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    csrf(request,db,x_csrf_token)
    try: return await SQLiteDataService(db).mutate_process_aware(bot,database_id,table_name,user,"delete",key=payload.key,token=payload.concurrency_token,confirmation=payload.confirmation)
    except SQLiteDataError as exc: raise failure(exc)
