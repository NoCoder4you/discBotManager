from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import secrets
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.models import Backup, BackupType, Bot, OperationStatus, User, VerificationStatus, utcnow

DEFAULT_EXCLUDES = (
    ".env", ".env.*", "*.token", "token", "tokens", "*secret*", "*credential*",
    "venv", "venv/**", ".venv", ".venv/**", "__pycache__", "**/__pycache__/**",
    ".pytest_cache", ".pytest_cache/**", ".git", ".git/**", "logs", "logs/**", "backups", "backups/**",
)


class BackupError(RuntimeError): pass
class BackupConflict(BackupError): pass
class BackupNotRestorable(BackupError): pass
class InsufficientStorage(BackupError): pass


class BotDataLocks:
    """Process-local, non-blocking bot data locks reusable by future editors."""
    def __init__(self): self._guard=threading.Lock(); self._locks:dict[str,threading.Lock]={}
    @contextmanager
    def acquire(self,bot_id:str):
        with self._guard: lock=self._locks.setdefault(bot_id,threading.Lock())
        if not lock.acquire(blocking=False): raise BackupConflict("Another data operation is already running for this bot")
        try: yield
        finally: lock.release()


bot_data_locks=BotDataLocks()


def _sha256(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


class BackupService:
    """Builds, verifies, previews, retains and atomically restores bot-scoped snapshots."""
    def __init__(self,db:Session,settings:Settings|None=None): self.db=db; self.settings=settings or get_settings()
    def source_root(self,bot:Bot)->Path:
        folder=Path(bot.folder).resolve(strict=True)
        if len(bot.data_roots or []) != 1: raise BackupError("Exactly one configured bot data root is required")
        relative=Path(bot.data_roots[0])
        if relative.is_absolute(): raise BackupError("Configured data root must be relative")
        source=(folder/relative).resolve(strict=True)
        if not source.is_dir() or not source.is_relative_to(folder): raise BackupError("Configured data root escapes the bot folder")
        return source
    def store_root(self,bot:Bot)->Path:
        central=Path(self.settings.backup_root).resolve()
        source=self.source_root(bot)
        central.mkdir(parents=True,exist_ok=True)
        if central==source or central.is_relative_to(source): raise BackupError("Backup root must be outside live bot data")
        root=(central/bot.id).resolve()
        if not root.is_relative_to(central): raise BackupError("Invalid bot backup root")
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        return root
    def backup_dir(self,backup:Backup)->Path:
        bot=self.db.get(Bot,backup.bot_id)
        if not bot: raise BackupError("Backup bot is unavailable")
        path=(self.store_root(bot)/backup.public_id).resolve()
        if not path.is_relative_to(self.store_root(bot)): raise BackupError("Invalid backup path")
        return path
    def _included(self,relative:PurePosixPath,bot:Bot)->bool:
        name=relative.as_posix(); parts=relative.parts
        patterns=tuple(bot.backup_include or ["**/*"])
        included=any(fnmatch.fnmatch(name,p) or (p=="**/*") for p in patterns)
        excluded=DEFAULT_EXCLUDES+tuple(bot.backup_exclude or [])
        return included and not any(fnmatch.fnmatch(name,p) or fnmatch.fnmatch(relative.name,p) or any(fnmatch.fnmatch(part,p) for part in parts) for p in excluded)
    def _files(self,bot:Bot)->Iterator[tuple[Path,PurePosixPath]]:
        root=self.source_root(bot)
        for directory,dirnames,filenames in os.walk(root,followlinks=False):
            base=Path(directory)
            safe_dirs=[]
            for name in dirnames:
                path=base/name; relative=PurePosixPath(path.relative_to(root).as_posix())
                if path.is_symlink() or not self._included(relative,bot): continue
                if not path.resolve().is_relative_to(root): continue
                safe_dirs.append(name)
            dirnames[:]=safe_dirs
            for name in filenames:
                path=base/name; relative=PurePosixPath(path.relative_to(root).as_posix())
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root) or not self._included(relative,bot): continue
                yield path,relative
    def _space_check(self,path:Path,required:int)->None:
        free=shutil.disk_usage(path).free; reserve=self.settings.backup_min_free_mb*1024*1024
        if free < required+reserve: raise InsufficientStorage(f"Insufficient storage: only {free//(1024*1024)} MB free")
    def _allocate_id(self)->str:
        # Random IDs remain unique even after retention deletes metadata and avoid
        # count/max races across multiple FastAPI workers.
        while True:
            candidate=f"BKP-{secrets.token_hex(8).upper()}"
            if not self.db.scalar(select(Backup.id).where(Backup.public_id==candidate)): return candidate
    def create(self,bot:Bot,actor:User|None,backup_type:BackupType=BackupType.MANUAL,reason:str|None=None,protected:bool=False,operation=None)->Backup:
        source=self.source_root(bot); store=self.store_root(bot)
        files=list(self._files(bot)); total=sum(path.stat().st_size for path,_ in files); maximum=self.settings.backup_max_size_mb*1024*1024
        if total>maximum: raise BackupError(f"Backup exceeds the configured {self.settings.backup_max_size_mb} MB limit")
        self._space_check(store,total*2)
        own_operation=operation is None
        if operation is None: operation=create_operation(self.db,"activity",user_id=actor.id if actor else None,bot_id=bot.id,event_metadata={"action":"backup.create"})
        operation.status=OperationStatus.RUNNING; self.db.flush()
        backup=Backup(public_id=self._allocate_id(),bot_id=bot.id,created_by_id=actor.id if actor else None,backup_type=backup_type,reason=reason,source_version=bot.source_version,protected=protected,operation_id=operation.public_id)
        self.db.add(backup); self.db.flush()
        temporary=Path(tempfile.mkdtemp(prefix=f".{backup.public_id}-",dir=store)); final=store/backup.public_id
        manifest={"backup_id":backup.public_id,"bot_id":bot.id,"created_at":backup.created_at.astimezone(timezone.utc).isoformat(),"source_version":bot.source_version,"files":[]}
        try:
            archive=temporary/backup.archive_name
            with tarfile.open(archive,"w:gz",format=tarfile.PAX_FORMAT) as tar:
                for path,relative in files:
                    snapshot=path
                    if path.suffix.lower() in {".db",".sqlite",".sqlite3"}:
                        snapshot=temporary/f".sqlite-{len(manifest['files'])}.db"
                        self._sqlite_backup(path,snapshot)
                    size=snapshot.stat().st_size
                    manifest["files"].append({"path":relative.as_posix(),"size":size,"sha256":_sha256(snapshot)})
                    tar.add(snapshot,arcname=relative.as_posix(),recursive=False,filter=lambda info:self._safe_tar_info(info))
            (temporary/backup.manifest_name).write_text(json.dumps(manifest,indent=2,sort_keys=True),encoding="utf-8")
            backup.size_bytes=archive.stat().st_size; backup.file_count=len(files)
            self._verify_paths(archive,temporary/backup.manifest_name,bot.id,validate_content=True)
            os.replace(temporary,final); backup.verification_status=VerificationStatus.VERIFIED
            operation.status=OperationStatus.COMPLETED; operation.completed_at=utcnow()
            event=DomainEvent(EventType.BACKUP_CREATED,actor,bot.id,{"backup_id":backup.public_id,"type":backup_type.value,"reason":reason,"operation_id":operation.public_id})
            EventBus(self.db).publish(event); AuditService(self.db).record(event,"success",backup.public_id,operation.public_id)
            EventBus(self.db).publish(DomainEvent(EventType.BACKUP_VERIFIED,actor,bot.id,{"backup_id":backup.public_id}))
            self.db.commit()
            # Count-based retention runs after a snapshot is fully finalized; cleanup
            # failures never turn an already verified backup into a false failure.
            try: self.retention_cleanup(bot)
            except Exception: pass
            return backup
        except Exception as exc:
            shutil.rmtree(temporary,ignore_errors=True); backup.verification_status=VerificationStatus.FAILED; backup.verification_error=str(exc)[:255]
            operation.status=OperationStatus.FAILED; operation.completed_at=utcnow(); operation.error=str(exc)[:255]
            EventBus(self.db).publish(DomainEvent(EventType.BACKUP_VERIFICATION_FAILED,actor,bot.id,{"backup_id":backup.public_id}))
            self.db.commit()
            raise
    @staticmethod
    def _safe_tar_info(info:tarfile.TarInfo):
        info.uid=info.gid=0; info.uname=info.gname=""; info.mode &= 0o777
        if not info.isfile(): raise BackupError("Only regular files may be archived")
        return info
    def _verify_paths(self,archive:Path,manifest_path:Path,bot_id:str,validate_content:bool=False)->dict:
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("bot_id")!=bot_id or not isinstance(manifest.get("files"),list): raise BackupError("Backup manifest is incompatible")
        expected={item["path"]:item for item in manifest["files"]}; seen=set()
        with tarfile.open(archive,"r:gz") as tar:
            members=tar.getmembers()
            for member in members:
                path=PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not member.isfile() or member.issym() or member.islnk(): raise BackupError("Unsafe archive member")
                item=expected.get(path.as_posix())
                if not item or member.size!=item["size"]: raise BackupError("Manifest does not match archive")
                stream=tar.extractfile(member); digest=hashlib.sha256()
                suffix=path.suffix.lower(); content=bytearray() if validate_content and suffix in {".json",".db",".sqlite",".sqlite3"} else None
                for chunk in iter(lambda:stream.read(1024*1024),b""):
                    digest.update(chunk)
                    if content is not None: content.extend(chunk)
                if digest.hexdigest()!=item["sha256"]: raise BackupError("Backup checksum mismatch")
                if content is not None and suffix==".json": json.loads(content)
                if content is not None and suffix in {".db",".sqlite",".sqlite3"}: self._sqlite_integrity(bytes(content))
                seen.add(path.as_posix())
        if seen!=set(expected): raise BackupError("Backup files are missing")
        return manifest
    @staticmethod
    def _sqlite_integrity(content:bytes)->None:
        fd,name=tempfile.mkstemp(suffix=".db")
        try:
            os.write(fd,content); os.close(fd); fd=-1
            with sqlite3.connect(name) as conn:
                result=conn.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0]!="ok": raise BackupError("SQLite integrity check failed")
        finally:
            if fd>=0: os.close(fd)
            Path(name).unlink(missing_ok=True)
    @staticmethod
    def _sqlite_backup(source:Path,destination:Path)->None:
        """Use SQLite's online snapshot API instead of copying a live database."""
        with sqlite3.connect(f"file:{source}?mode=ro",uri=True) as current, sqlite3.connect(destination) as snapshot:
            current.backup(snapshot)
            result=snapshot.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0]!="ok": raise BackupError("SQLite integrity check failed")
    def verify(self,backup:Backup)->bool:
        try:
            root=self.backup_dir(backup); self._verify_paths(root/backup.archive_name,root/backup.manifest_name,backup.bot_id,True)
            backup.verification_status=VerificationStatus.VERIFIED; backup.verification_error=None; self.db.commit(); return True
        except Exception as exc:
            backup.verification_status=VerificationStatus.FAILED; backup.verification_error=str(exc)[:255]; self.db.commit(); return False
    def manifest(self,backup:Backup)->dict:
        root=self.backup_dir(backup); return self._verify_paths(root/backup.archive_name,root/backup.manifest_name,backup.bot_id)
    def preview(self,bot:Bot,backup:Backup)->dict:
        if backup.bot_id!=bot.id: raise BackupError("Resource not found")
        manifest=self.manifest(backup); current={rel.as_posix():{"size":path.stat().st_size,"sha256":_sha256(path)} for path,rel in self._files(bot)}
        changes=[]; archived={x["path"]:x for x in manifest["files"]}
        for path in sorted(set(current)|set(archived)):
            if path not in current: status="added"
            elif path not in archived: status="removed"
            elif current[path]["sha256"]!=archived[path]["sha256"]: status="changed"
            else: status="unchanged"
            changes.append({"path":path,"status":status,"backup_size":archived.get(path,{}).get("size"),"current_size":current.get(path,{}).get("size")})
        return {"backup_id":backup.public_id,"eligible":backup.verification_status is VerificationStatus.VERIFIED,"files":changes}
    def _extract(self,backup:Backup,destination:Path)->None:
        root=self.backup_dir(backup); manifest=self._verify_paths(root/backup.archive_name,root/backup.manifest_name,backup.bot_id,True)
        destination.mkdir(parents=True)
        with tarfile.open(root/backup.archive_name,"r:gz") as tar:
            for member in tar.getmembers():
                relative=PurePosixPath(member.name)
                target=(destination/Path(*relative.parts)).resolve()
                if not target.is_relative_to(destination.resolve()) or not member.isfile(): raise BackupError("Unsafe archive member")
                target.parent.mkdir(parents=True,exist_ok=True)
                stream=tar.extractfile(member)
                with target.open("xb") as output: shutil.copyfileobj(stream,output,1024*1024)
                os.chmod(target,member.mode & 0o777)
        for item in manifest["files"]:
            if _sha256(destination/item["path"])!=item["sha256"]: raise BackupError("Staging checksum mismatch")
    def restore(self,bot:Bot,backup:Backup,actor:User,operation=None,safety:Backup|None=None)->tuple[Backup,bool]:
        if backup.bot_id!=bot.id: raise BackupNotRestorable("Resource not found")
        if backup.verification_status is not VerificationStatus.VERIFIED or not self.verify(backup): raise BackupNotRestorable("Backup is not verified and cannot be restored")
        with bot_data_locks.acquire(bot.id):
            operation=operation or create_operation(self.db,"activity",user_id=actor.id,bot_id=bot.id,event_metadata={"action":"backup.restore","backup_id":backup.public_id})
            operation.status=OperationStatus.RUNNING; self.db.flush()
            started=DomainEvent(EventType.BACKUP_RESTORE_STARTED,actor,bot.id,{"backup_id":backup.public_id,"operation_id":operation.public_id}); EventBus(self.db).publish(started); AuditService(self.db).record(started,"requested",backup.public_id,operation.public_id); self.db.commit()
            if safety is None: safety=self.create(bot,actor,BackupType.PRE_RESTORE,f"Before restoring {backup.public_id}",protected=True)
            if safety.bot_id!=bot.id or safety.backup_type is not BackupType.PRE_RESTORE or safety.verification_status is not VerificationStatus.VERIFIED: raise BackupNotRestorable("A verified pre-restore safety backup is required")
            source=self.source_root(bot); parent=source.parent; self._space_check(parent,backup.size_bytes*2)
            staging=Path(tempfile.mkdtemp(prefix=f".restore-{bot.id}-",dir=parent)); staging.rmdir()
            rollback=parent/f".rollback-{bot.id}-{operation.public_id}"
            swapped=False
            try:
                self._extract(backup,staging)
                os.replace(source,rollback); swapped=True; os.replace(staging,source)
                # Validate the final tree against the immutable backup manifest.
                for item in self.manifest(backup)["files"]:
                    target=source/item["path"]
                    if not target.is_file() or _sha256(target)!=item["sha256"]: raise BackupError("Final restore validation failed")
                shutil.rmtree(rollback); backup.restore_count+=1; safety.protected=False
                operation.status=OperationStatus.COMPLETED; operation.completed_at=utcnow()
                event=DomainEvent(EventType.BACKUP_RESTORED,actor,bot.id,{"backup_id":backup.public_id,"safety_backup_id":safety.public_id,"operation_id":operation.public_id,"rollback":False}); EventBus(self.db).publish(event); AuditService(self.db).record(event,"success",backup.public_id,operation.public_id); self.db.commit(); return safety,True
            except Exception as exc:
                rollback_ok=not swapped
                try:
                    if swapped:
                        failed=parent/f".failed-restore-{bot.id}-{operation.public_id}"
                        if source.exists(): os.replace(source,failed)
                        os.replace(rollback,source); shutil.rmtree(failed,ignore_errors=True); rollback_ok=True
                except Exception: rollback_ok=False
                shutil.rmtree(staging,ignore_errors=True); safety.protected=not rollback_ok
                operation.status=OperationStatus.FAILED; operation.completed_at=utcnow(); operation.error=str(exc)[:255]
                event=DomainEvent(EventType.BACKUP_RESTORE_FAILED,actor,bot.id,{"backup_id":backup.public_id,"operation_id":operation.public_id,"rollback_succeeded":rollback_ok}); EventBus(self.db).publish(event); AuditService(self.db).record(event,"failed",backup.public_id,operation.public_id); self.db.commit()
                raise BackupError(f"Restore failed; rollback {'succeeded' if rollback_ok else 'also failed'}") from exc
    def pin(self,backup:Backup,actor:User,pinned:bool):
        backup.pinned=pinned; event=DomainEvent(EventType.BACKUP_PINNED if pinned else EventType.BACKUP_UNPINNED,actor,backup.bot_id,{"backup_id":backup.public_id}); EventBus(self.db).publish(event); AuditService(self.db).record(event,"success",backup.public_id); self.db.commit()
    def retention_cleanup(self,bot:Bot)->list[str]:
        limits={BackupType.HOURLY:self.settings.backup_retention_hourly,BackupType.DAILY:self.settings.backup_retention_daily,BackupType.WEEKLY:self.settings.backup_retention_weekly,BackupType.MONTHLY:self.settings.backup_retention_monthly,BackupType.MANUAL:self.settings.backup_retention_manual}
        removed=[]
        for kind,limit in limits.items():
            if limit<=0: continue
            rows=list(self.db.scalars(select(Backup).where(Backup.bot_id==bot.id,Backup.backup_type==kind).order_by(Backup.created_at.desc())))
            for row in rows[limit:]:
                if row.pinned or row.protected: continue
                root=self.backup_dir(row); shutil.rmtree(root); removed.append(row.public_id); self.db.delete(row)
        if removed: EventBus(self.db).publish(DomainEvent(EventType.BACKUP_RETENTION_CLEANUP,None,bot.id,{"deleted_count":len(removed)})); self.db.commit()
        return removed
