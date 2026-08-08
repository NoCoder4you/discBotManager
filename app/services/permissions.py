from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Bot, BotAssignment, Effect, Permission, PlatformRole, RolePermission, User, UserPermission

class PermissionService:
    """Owner > explicit deny > explicit grant > assigned role > default deny."""
    def __init__(self, db: Session): self.db=db
    def has(self,user:User,key:str,bot_id:str|None=None)->bool:
        if not user.enabled: return False
        if user.platform_role is PlatformRole.OWNER: return True
        permission=self.db.scalar(select(Permission).where(Permission.key==key))
        if not permission: return False
        overrides=self.db.scalars(select(UserPermission).where(UserPermission.user_id==user.id,UserPermission.permission_id==permission.id,UserPermission.bot_id==bot_id)).all()
        if any(x.effect is Effect.DENY for x in overrides): return False
        if any(x.effect is Effect.GRANT for x in overrides): return True
        if bot_id is None: return False
        assignment=self.db.scalar(select(BotAssignment).where(BotAssignment.user_id==user.id,BotAssignment.bot_id==bot_id))
        return bool(assignment and self.db.scalar(select(RolePermission).where(RolePermission.role_id==assignment.role_id,RolePermission.permission_id==permission.id)))
    def visible_bots(self,user:User):
        if user.platform_role is PlatformRole.OWNER: return list(self.db.scalars(select(Bot).order_by(Bot.display_name)))
        return list(self.db.scalars(select(Bot).join(BotAssignment).where(BotAssignment.user_id==user.id).order_by(Bot.display_name)))
    def visible_bot(self,user:User,bot_id:str):
        bot=self.db.get(Bot,bot_id)
        return bot if bot and self.has(user,"bot.view",bot_id) else None
