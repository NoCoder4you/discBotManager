from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.security import session_from_request
from app.models import User
from app.services.permissions import PermissionService
def current_user(request:Request,db:Session=Depends(get_db))->User:
    session=session_from_request(request,db)
    if not session or not session.user or not session.user.enabled: raise HTTPException(401,"Authentication required")
    return session.user
def requires_platform_permission(key:str):
    def dependency(user:User=Depends(current_user),db:Session=Depends(get_db)):
        if not PermissionService(db).has(user,key): raise HTTPException(403,"Forbidden")
        return user
    return dependency
def requires_bot_permission(key:str):
    def dependency(bot_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
        bot=PermissionService(db).visible_bot(user,bot_id)
        if not bot or not PermissionService(db).has(user,key,bot_id): raise HTTPException(404,"Resource not found")
        return bot
    return dependency
