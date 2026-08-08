from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.auth.dependencies import current_user, requires_bot_permission
from app.database import get_db
from app.models import Bot, PlatformRole, User
from app.services.permissions import PermissionService
from app.core.security import session_from_request
router=APIRouter()
@router.get("/",response_class=HTMLResponse)
def home(request:Request): return request.app.state.templates.TemplateResponse(request,"login.html",{})
@router.get("/dashboard",response_class=HTMLResponse)
def dashboard(request:Request,user:User=Depends(current_user),db:Session=Depends(get_db)):
    bots=PermissionService(db).visible_bots(user); session=session_from_request(request,db); return request.app.state.templates.TemplateResponse(request,"dashboard.html",{"user":user,"bots":bots,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token})
@router.get("/bots/{bot_id}",response_class=HTMLResponse)
def bot_detail(request:Request,bot:Bot=Depends(requires_bot_permission("bot.view")),user:User=Depends(current_user),db:Session=Depends(get_db)):
    session=session_from_request(request,db); return request.app.state.templates.TemplateResponse(request,"bot.html",{"user":user,"bot":bot,"is_owner":user.platform_role is PlatformRole.OWNER,"csrf_token":session.csrf_token})
