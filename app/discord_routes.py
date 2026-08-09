from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user, requires_bot_permission
from app.core.security import require_csrf, session_from_request
from app.database import get_db
from app.models import Bot, User
from app.services.guild_snapshots import GuildSnapshotService
from app.services.process_manager import SupervisorUnavailable, process_manager

router=APIRouter()
def service(db): return GuildSnapshotService(db)
def guild_or_404(db,bot_id,guild_id):
    row=next((x for x in service(db).rows(bot_id) if x.guild_id==guild_id),None)
    if not row: raise HTTPException(404,"Resource not found")
    return row
@router.get("/api/bots/{bot_id}/servers")
def api_servers(bot:Bot=Depends(requires_bot_permission("servers.view")),db:Session=Depends(get_db)): return {"servers":[service(db).view(x) for x in service(db).rows(bot.id)]}
@router.get("/api/bots/{bot_id}/servers/{guild_id}")
def api_server(guild_id:str,bot:Bot=Depends(requires_bot_permission("servers.view")),db:Session=Depends(get_db)): return service(db).view(guild_or_404(db,bot.id,guild_id))
@router.get("/api/bots/{bot_id}/servers/{guild_id}/roles")
def api_roles(guild_id:str,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=250),bot:Bot=Depends(requires_bot_permission("servers.view")),db:Session=Depends(get_db)):
    roles=sorted(service(db).view(guild_or_404(db,bot.id,guild_id))["roles"],key=lambda x:(x["position"],int(x["role_id"])),reverse=True); return {"roles":roles[offset:offset+limit],"total":len(roles)}
@router.get("/api/bots/{bot_id}/servers/{guild_id}/channels")
def api_channels(guild_id:str,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=250),bot:Bot=Depends(requires_bot_permission("servers.view")),db:Session=Depends(get_db)):
    channels=sorted(service(db).view(guild_or_404(db,bot.id,guild_id))["channels"],key=lambda x:(x.get("category_id") or "",x["position"])); return {"channels":channels[offset:offset+limit],"total":len(channels)}
@router.get("/api/bots/{bot_id}/servers/{guild_id}/diagnostics")
def api_diagnostics(guild_id:str,status:str|None=None,severity:str|None=None,q:str|None=Query(None,max_length=100),bot:Bot=Depends(requires_bot_permission("servers.view")),db:Session=Depends(get_db)):
    items=service(db).view(guild_or_404(db,bot.id,guild_id))["diagnostics"]
    if status: items=[x for x in items if x["status"]==status.upper()]
    if severity: items=[x for x in items if x["severity"]==severity.upper()]
    if q: items=[x for x in items if q.lower() in " ".join(str(x.get(k,"")) for k in ("title","capability_name","target_id")).lower()]
    return {"diagnostics":items}
@router.get("/bots/{bot_id}/servers",response_class=HTMLResponse)
def servers_page(request:Request,bot:Bot=Depends(requires_bot_permission("servers.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    return request.app.state.templates.TemplateResponse(request,"servers.html",{"user":user,"bot":bot,"servers":[service(db).view(x) for x in service(db).rows(bot.id)],"csrf_token":session_from_request(request,db).csrf_token})
@router.get("/bots/{bot_id}/servers/{guild_id}",response_class=HTMLResponse)
def server_page(request:Request,guild_id:str,bot:Bot=Depends(requires_bot_permission("servers.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    return request.app.state.templates.TemplateResponse(request,"server_detail.html",{"user":user,"bot":bot,"guild":service(db).view(guild_or_404(db,bot.id,guild_id)),"csrf_token":session_from_request(request,db).csrf_token})
@router.post("/api/bots/{bot_id}/servers/refresh")
async def refresh(request:Request,csrf_token:str=Form(...),bot:Bot=Depends(requires_bot_permission("servers.view")),db:Session=Depends(get_db)):
    require_csrf(request,session_from_request(request,db),csrf_token)
    try: result=await process_manager.client.request_guild_snapshot(bot.id)
    except SupervisorUnavailable as exc: raise HTTPException(503,"Live Discord data unavailable") from exc
    return JSONResponse(result,202)
