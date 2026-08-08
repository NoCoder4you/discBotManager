from app.models import Bot, BotAssignment, Effect, Permission, PlatformRole, Role, RolePermission, User, UserPermission
from app.services.permissions import PermissionService
def setup(db):
    view=Permission(key="bot.view"); restart=Permission(key="bot.restart"); viewer=Role(key="viewer",name="Viewer"); bot=Bot(display_name="Secret",folder=".",entry_file="bot.py"); user=User(discord_id="2",username="alex",display_name="Alex"); db.add_all([view,restart,viewer,bot,user]); db.flush(); db.add_all([RolePermission(role_id=viewer.id,permission_id=view.id),BotAssignment(user_id=user.id,bot_id=bot.id,role_id=viewer.id)]); db.commit(); return user,bot,view,restart
def test_viewer_cannot_operator_action(db):
    user,bot,_,_=setup(db); service=PermissionService(db)
    assert service.has(user,"bot.view",bot.id); assert not service.has(user,"bot.restart",bot.id)
def test_owner_has_every_permission_even_unregistered(db):
    owner=User(discord_id="1",username="owner",display_name="Owner",platform_role=PlatformRole.OWNER); db.add(owner); db.commit()
    assert PermissionService(db).has(owner,"anything",None)
def test_explicit_deny_precedes_grant_and_role(db):
    user,bot,view,_=setup(db); db.add_all([UserPermission(user_id=user.id,bot_id=bot.id,permission_id=view.id,effect=Effect.GRANT),UserPermission(user_id=user.id,bot_id=bot.id,permission_id=view.id,effect=Effect.DENY)]); db.commit()
    assert not PermissionService(db).has(user,"bot.view",bot.id)
def test_explicit_grant_precedes_missing_role_permission(db):
    user,bot,_,restart=setup(db); db.add(UserPermission(user_id=user.id,bot_id=bot.id,permission_id=restart.id,effect=Effect.GRANT)); db.commit()
    assert PermissionService(db).has(user,"bot.restart",bot.id)
def test_unassigned_bot_is_not_visible_or_discoverable(db):
    user,_,_,_=setup(db); hidden=Bot(display_name="Hidden",folder=".",entry_file="x.py"); db.add(hidden); db.commit()
    service=PermissionService(db); assert hidden not in service.visible_bots(user); assert service.visible_bot(user,hidden.id) is None; assert service.visible_bot(user,"not-a-real-id") is None
def test_disabled_user_is_denied(db):
    user,bot,_,_=setup(db); user.enabled=False; db.commit(); assert not PermissionService(db).has(user,"bot.view",bot.id)
