from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Permission, Role, RolePermission


class DangerLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class PermissionDefinition:
    key: str
    name: str
    description: str
    category: str
    danger: DangerLevel = DangerLevel.LOW


def _p(key: str, name: str, category: str, danger: DangerLevel = DangerLevel.LOW) -> PermissionDefinition:
    return PermissionDefinition(key, name, f"Allows {name.lower()} for an assigned bot.", category, danger)


PERMISSIONS = (
    _p("bot.view", "View Bot", "Bot Control"), _p("bot.start", "Start Bot", "Bot Control", DangerLevel.HIGH),
    _p("bot.stop", "Stop Bot", "Bot Control", DangerLevel.HIGH), _p("bot.restart", "Restart Bot", "Bot Control", DangerLevel.HIGH),
    _p("bot.maintenance.enable", "Enable Maintenance", "Bot Control", DangerLevel.HIGH), _p("bot.maintenance.disable", "Disable Maintenance", "Bot Control", DangerLevel.HIGH),
    _p("console.view", "View Console", "Console"), _p("console.clear", "Clear Console", "Console", DangerLevel.MEDIUM), _p("console.download", "Download Console", "Console", DangerLevel.MEDIUM),
    _p("commands.view", "View Commands", "Commands"), _p("commands.sync", "Sync Commands", "Commands", DangerLevel.MEDIUM),
    _p("cogs.view", "View Cogs", "Cogs"), _p("cogs.load", "Load Cog", "Cogs", DangerLevel.HIGH), _p("cogs.unload", "Unload Cog", "Cogs", DangerLevel.HIGH), _p("cogs.reload", "Reload Cog", "Cogs", DangerLevel.MEDIUM), _p("cogs.reload_all", "Reload All Cogs", "Cogs", DangerLevel.HIGH),
    _p("files.view", "View Files", "Files", DangerLevel.MEDIUM), _p("files.edit", "Edit Files", "Files", DangerLevel.CRITICAL),
    _p("config.view", "View Configuration", "Configuration", DangerLevel.MEDIUM), _p("config.edit", "Edit Configuration", "Configuration", DangerLevel.CRITICAL),
    _p("database.view", "View Database", "Database", DangerLevel.HIGH), _p("database.edit", "Edit Database", "Database", DangerLevel.CRITICAL),
    _p("backups.view", "View Backups", "Backups"), _p("backups.create", "Create Backup", "Backups", DangerLevel.MEDIUM), _p("backups.restore", "Restore Backup", "Backups", DangerLevel.CRITICAL),
    _p("scheduler.view", "View Scheduler", "Scheduler"), _p("scheduler.run", "Run Scheduled Task", "Scheduler", DangerLevel.HIGH), _p("scheduler.edit", "Edit Scheduler", "Scheduler", DangerLevel.HIGH),
    _p("errors.view", "View Errors", "Errors"), _p("errors.acknowledge", "Acknowledge Errors", "Errors", DangerLevel.MEDIUM),
    _p("servers.view", "View Servers", "Servers"), _p("activity.view", "View Activity", "Activity"),
)

VIEWER = {"bot.view", "console.view", "commands.view", "cogs.view", "errors.view", "servers.view", "activity.view"}
OPERATOR = VIEWER | {"bot.start", "bot.restart", "commands.sync", "cogs.reload", "cogs.reload_all", "backups.view", "backups.create", "scheduler.view", "scheduler.run"}
ADMINISTRATOR = {p.key for p in PERMISSIONS}
ROLE_MAPPINGS = {"viewer": VIEWER, "operator": OPERATOR, "administrator": ADMINISTRATOR}


def seed_catalog(db: Session) -> None:
    """Idempotently reconciles canonical keys and mappings; arbitrary keys are never accepted."""
    permissions = {row.key: row for row in db.scalars(select(Permission))}
    for definition in PERMISSIONS:
        row = permissions.get(definition.key)
        description = f"{definition.name}|{definition.category}|{definition.danger.value}|{definition.description}"
        if row is None:
            row = Permission(key=definition.key, description=description); db.add(row); db.flush(); permissions[definition.key] = row
        else: row.description = description
    roles = {row.key: row for row in db.scalars(select(Role).where(Role.scope == "bot"))}
    for key, mapping in ROLE_MAPPINGS.items():
        role = roles.get(key)
        if role is None:
            role = Role(key=key, name=key.title(), scope="bot"); db.add(role); db.flush()
        existing = {x.permission_id for x in db.scalars(select(RolePermission).where(RolePermission.role_id == role.id))}
        for permission_key in mapping:
            permission_id = permissions[permission_key].id
            if permission_id not in existing: db.add(RolePermission(role_id=role.id, permission_id=permission_id))
    db.commit()
