import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.database import Base, get_db
from app.main import create_app
from app.models import Backup, BackupType, Bot, BotAssignment, Effect, Permission, PlatformRole, Role, Session, User, UserPermission, VerificationStatus
from app.services.backups import BackupError, BackupService, InsufficientStorage
from app.services.catalog import seed_catalog


def settings(root,**values):
    values.setdefault("backup_min_free_mb",0)
    return Settings(app_secret="x"*32,environment="test",supervisor_secret="y"*32,backup_root=str(root),**values)

def objects(db,tmp_path):
    live=tmp_path/"live"; live.mkdir(); (live/"state.json").write_text('{"points": 140}')
    owner=User(discord_id="1",username="owner",display_name="Owner",platform_role=PlatformRole.OWNER)
    bot=Bot(id="events",display_name="Events",folder=str(tmp_path),entry_file="bot.py",data_roots=["live"],backup_include=["**/*"])
    db.add_all([owner,bot]); db.commit(); return owner,bot,live

def test_create_manifest_checksums_excludes_secrets_symlinks_and_store(db,tmp_path):
    owner,bot,live=objects(db,tmp_path); (live/".env").write_text("TOKEN=fake"); (live/"management_secret.txt").write_text("fake")
    outside=tmp_path/"outside.txt"; outside.write_text("outside"); (live/"escape").symlink_to(outside)
    service=BackupService(db,settings(tmp_path/"store")); backup=service.create(bot,owner,reason="Before leaderboard changes")
    assert backup.public_id.startswith("BKP-") and len(backup.public_id)==20 and backup.verification_status is VerificationStatus.VERIFIED and backup.operation_id.startswith("ACT-")
    root=service.backup_dir(backup); manifest=json.loads((root/"manifest.json").read_text()); names={x["path"] for x in manifest["files"]}
    assert names=={"state.json"} and len(manifest["files"][0]["sha256"])==64 and root.is_relative_to(tmp_path/"store") and not root.is_relative_to(live)
    with tarfile.open(root/"data.tar.gz") as archive: assert archive.getnames()==["state.json"]

def test_verification_corruption_and_unsafe_archive_block_restore(db,tmp_path):
    owner,bot,_=objects(db,tmp_path); service=BackupService(db,settings(tmp_path/"store")); backup=service.create(bot,owner)
    archive=service.backup_dir(backup)/backup.archive_name; archive.write_bytes(b"corrupt")
    assert not service.verify(backup) and backup.verification_status is VerificationStatus.FAILED
    with pytest.raises(BackupError): service.restore(bot,backup,owner)

def test_restore_preview_safety_backup_and_atomic_result(db,tmp_path):
    owner,bot,live=objects(db,tmp_path); service=BackupService(db,settings(tmp_path/"store")); backup=service.create(bot,owner)
    (live/"state.json").write_text('{"points": 155}'); (live/"new.bin").write_bytes(b"new")
    preview=service.preview(bot,backup); assert {x["path"]:x["status"] for x in preview["files"]}=={"new.bin":"removed","state.json":"changed"}
    safety,ok=service.restore(bot,backup,owner); assert ok and json.loads((live/"state.json").read_text())["points"]==140 and not (live/"new.bin").exists()
    assert safety.backup_type is BackupType.PRE_RESTORE and safety.verification_status is VerificationStatus.VERIFIED and not safety.protected

def test_cross_bot_path_escape_invalid_json_and_disk_limit(db,tmp_path,monkeypatch):
    owner,bot,live=objects(db,tmp_path); other_root=tmp_path/"other"; other_root.mkdir(); other=Bot(id="pay",display_name="Pay",folder=str(tmp_path),entry_file="bot.py",data_roots=["other"]); db.add(other); db.commit()
    service=BackupService(db,settings(tmp_path/"store")); backup=service.create(bot,owner)
    with pytest.raises(BackupError): service.restore(other,backup,owner)
    bot.data_roots=["../"]; db.commit()
    with pytest.raises(BackupError): service.create(bot,owner)
    bot.data_roots=["live"]; (live/"bad.json").write_text("not json"); db.commit()
    with pytest.raises(Exception): service.create(bot,owner)
    monkeypatch.setattr("app.services.backups.shutil.disk_usage",lambda _:type("Usage",(),{"free":0})())
    with pytest.raises(InsufficientStorage): BackupService(db,settings(tmp_path/"space",backup_min_free_mb=1)).create(bot,owner)

def test_retention_preserves_pinned_and_protected(db,tmp_path):
    owner,bot,_=objects(db,tmp_path); service=BackupService(db,settings(tmp_path/"store",backup_retention_hourly=1))
    first=service.create(bot,owner,BackupType.HOURLY); first.pinned=True; db.commit()
    second=service.create(bot,owner,BackupType.HOURLY); second.protected=True; db.commit()
    third=service.create(bot,owner,BackupType.HOURLY)
    # Newest is retained by policy; both older snapshots are retained by safety flags.
    assert service.retention_cleanup(bot)==[] and all(service.backup_dir(x).exists() for x in (first,second,third))
    first.pinned=False; second.protected=False; db.commit(); removed=service.retention_cleanup(bot)
    assert set(removed)=={first.public_id,second.public_id}


@pytest.fixture
def backup_app(tmp_path):
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool); Base.metadata.create_all(engine); factory=sessionmaker(engine,expire_on_commit=False)
    live=tmp_path/"live"; pay_live=tmp_path/"pay"; live.mkdir(); pay_live.mkdir(); (live/"state.json").write_text("{}")
    with factory() as db:
        seed_catalog(db); viewer=db.scalar(select(Role).where(Role.key=="viewer")); operator=db.scalar(select(Role).where(Role.key=="operator"))
        user=User(discord_id="2",username="user",display_name="User"); owner=User(discord_id="1",username="owner",display_name="Owner",platform_role=PlatformRole.OWNER)
        events=Bot(id="events",display_name="Events",folder=str(tmp_path),entry_file="bot.py",data_roots=["live"]); pay=Bot(id="pay",display_name="Pay",folder=str(tmp_path),entry_file="bot.py",data_roots=["pay"])
        db.add_all([user,owner,events,pay]); db.flush(); db.add(BotAssignment(user_id=user.id,bot_id=events.id,role_id=operator.id)); db.add_all([Session(id="user",user_id=user.id,csrf_token="csrf",expires_at=datetime.now(timezone.utc)+timedelta(hours=1)),Session(id="owner",user_id=owner.id,csrf_token="csrf",expires_at=datetime.now(timezone.utc)+timedelta(hours=1))]); db.commit()
    app=create_app()
    def override():
        with factory() as db: yield db
    app.dependency_overrides[get_db]=override
    from app.core.config import get_settings
    original=get_settings().backup_root; get_settings().backup_root=str(tmp_path/"store"); get_settings().backup_min_free_mb=0
    yield TestClient(app),factory
    get_settings().backup_root=original

def test_api_permissions_csrf_non_enumeration_and_explicit_deny(backup_app):
    client,factory=backup_app; cookies={"dbm_session":"user"}
    assert client.get("/api/bots/pay/backups",cookies=cookies).status_code==404
    assert client.post("/api/bots/events/backups",cookies=cookies,data={"csrf_token":"bad"}).status_code==403
    created=client.post("/api/bots/events/backups",cookies=cookies,data={"csrf_token":"csrf","reason":"safe"}); assert created.status_code==201
    backup_id=created.json()["backup_id"]
    assert client.get(f"/api/bots/pay/backups/{backup_id}/preview",cookies=cookies).status_code==404
    assert client.post(f"/api/bots/events/backups/{backup_id}/restore",cookies=cookies,data={"csrf_token":"csrf","confirmation":"EVENTS"}).status_code==404
    with factory() as db:
        user=db.scalar(select(User).where(User.discord_id=="2")); permission=db.scalar(select(Permission).where(Permission.key=="backups.view")); db.add(UserPermission(user_id=user.id,bot_id="events",permission_id=permission.id,effect=Effect.DENY)); db.commit()
    assert client.get("/api/bots/events/backups",cookies=cookies).status_code==404
