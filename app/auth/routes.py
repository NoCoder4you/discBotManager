import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.security import COOKIE, new_session, require_csrf, session_from_request
from app.database import get_db
from app.models import PlatformRole, User
from app.services.console_stream import console_subscriptions
router=APIRouter(prefix="/auth",tags=["auth"])
@router.get("/login")
def login(db:Session=Depends(get_db)):
    cfg=get_settings()
    if not cfg.discord_client_id: raise HTTPException(503,"Discord OAuth is not configured")
    session=new_session(db); session.oauth_state=secrets.token_urlsafe(32); db.commit()
    url="https://discord.com/oauth2/authorize?"+urlencode({"client_id":cfg.discord_client_id,"response_type":"code","redirect_uri":cfg.discord_redirect_uri,"scope":"identify","state":session.oauth_state})
    response=RedirectResponse(url); response.set_cookie(COOKIE,session.id,httponly=True,secure=cfg.secure_cookies,samesite="lax",max_age=43200); return response
@router.get("/callback")
async def callback(request:Request,code:str,state:str,db:Session=Depends(get_db)):
    cfg=get_settings(); session=session_from_request(request,db)
    if not session or not session.oauth_state or not secrets.compare_digest(session.oauth_state,state): raise HTTPException(400,"Invalid OAuth state")
    async with httpx.AsyncClient(timeout=10) as client:
        token=(await client.post("https://discord.com/api/oauth2/token",data={"client_id":cfg.discord_client_id,"client_secret":cfg.discord_client_secret,"grant_type":"authorization_code","code":code,"redirect_uri":cfg.discord_redirect_uri})); token.raise_for_status(); access=token.json()["access_token"]
        profile=(await client.get("https://discord.com/api/users/@me",headers={"Authorization":f"Bearer {access}"})); profile.raise_for_status(); data=profile.json()
    user=db.scalar(select(User).where(User.discord_id==data["id"]))
    if not user: user=User(discord_id=data["id"],username=data["username"],display_name=data.get("global_name") or data["username"],avatar=data.get("avatar")); db.add(user)
    user.username=data["username"]; user.display_name=data.get("global_name") or data["username"]; user.avatar=data.get("avatar"); user.last_login=datetime.now(timezone.utc)
    if user.discord_id==cfg.platform_owner_discord_id: user.platform_role=PlatformRole.OWNER; user.enabled=True
    if not user.enabled: raise HTTPException(403,"Account disabled")
    session.user=user; session.oauth_state=None; db.commit(); return RedirectResponse("/dashboard",status_code=303)
@router.post("/logout")
def logout(request:Request,csrf_token:str=Form(...),db:Session=Depends(get_db)):
    row=session_from_request(request,db)
    if not row: raise HTTPException(401,"Authentication required")
    require_csrf(request,row,csrf_token); user_id=row.user_id; db.delete(row); db.commit()
    if user_id: console_subscriptions.revoke(user_id)
    response=RedirectResponse("/",303); response.delete_cookie(COOKIE); return response
