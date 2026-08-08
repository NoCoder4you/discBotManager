import secrets
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session
from app.models import Session as UserSession

COOKIE="dbm_session"
def new_session(db:Session)->UserSession:
    row=UserSession(id=secrets.token_urlsafe(32),csrf_token=secrets.token_urlsafe(32),expires_at=datetime.now(timezone.utc)+timedelta(hours=12)); db.add(row); db.commit(); return row
def session_from_request(request:Request,db:Session):
    sid=request.cookies.get(COOKIE); row=db.get(UserSession,sid) if sid else None
    if not row or row.expires_at.replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc): return None
    return row
def require_csrf(request:Request,row:UserSession,token:str):
    if not secrets.compare_digest(row.csrf_token,token): raise HTTPException(403,"Invalid CSRF token")
