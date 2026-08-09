from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import ConfigField, DataSource
from app.adapters.registry import get_adapter
from app.core.config import Settings, get_settings
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.models import BackupType, Bot, DataVersion, OperationStatus, User, VerificationStatus, utcnow
from app.services.backups import BackupError, BackupService, bot_data_locks

VIEW_TYPES={".json",".txt",".md",".yaml",".yml",".toml",".ini"}
BLOCKED_DIRS={"venv",".venv","site-packages","__pycache__",".git","logs","backups"}
SENSITIVE_NAMES={".env","credentials.json","oauth.json","token","tokens","supervisor.json","supervisor-state.json","agent-secret.json","platform.db","platform.sqlite","platform.sqlite3"}
SENSITIVE_SUFFIXES={".pem",".key",".p12",".pfx"}
SNOWFLAKE=re.compile(r"^[0-9]{17,20}$")
COLOUR=re.compile(r"^#[0-9A-Fa-f]{6}$")

class DataError(RuntimeError): pass
class DataNotFound(DataError): pass
class DataConflict(DataError): pass
class DataValidationError(DataError):
    def __init__(self,message:str,errors:dict[str,str]|None=None): super().__init__(message); self.errors=errors or {}

def content_hash(content:bytes)->str: return hashlib.sha256(content).hexdigest()

class BotDataService:
    """Bot-rooted, allowlisted data access and transactional JSON/config mutation."""
    def __init__(self,db:Session,settings:Settings|None=None): self.db=db; self.settings=settings or get_settings(); self.backups=BackupService(db,self.settings)
    def root(self,bot:Bot)->Path: return self.backups.source_root(bot)
    def sources(self,bot:Bot,config:bool=False)->tuple[DataSource,...]:
        adapter=get_adapter(bot.adapter)
        return tuple(adapter.get_config_schema() if config else adapter.get_data_sources())
    def source(self,bot:Bot,source_id:str,config:bool=False)->DataSource:
        source=next((x for x in self.sources(bot,config) if x.id==source_id),None)
        if not source: raise DataNotFound("Resource not found")
        self.resolve(bot,source.path)
        return source
    def _sensitive(self,relative:PurePosixPath)->bool:
        lowered=[x.lower() for x in relative.parts]; name=lowered[-1] if lowered else ""
        return name in SENSITIVE_NAMES or Path(name).suffix in SENSITIVE_SUFFIXES or any(x in BLOCKED_DIRS for x in lowered) or "credential" in name or "secret" in name or name.endswith("token.json")
    def resolve(self,bot:Bot,relative:str,must_exist:bool=True)->Path:
        if not relative or "\x00" in relative or Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute() or "\\" in relative:
            raise DataNotFound("Resource not found")
        pure=PurePosixPath(relative)
        if ".." in pure.parts or self._sensitive(pure): raise DataNotFound("Resource not found")
        root=self.root(bot); candidate=root.joinpath(*pure.parts)
        try:
            # Reject all symlinks, including a symlinked parent, rather than merely
            # checking where the final target happens to point today.
            current=root
            for part in pure.parts:
                current=current/part
                if current.is_symlink(): raise DataNotFound("Resource not found")
            resolved=candidate.resolve(strict=must_exist)
        except (OSError,RuntimeError): raise DataNotFound("Resource not found") from None
        if not resolved.is_relative_to(root): raise DataNotFound("Resource not found")
        return resolved
    def list_directory(self,bot:Bot,relative:str=".")->list[dict]:
        directory=self.resolve(bot,relative)
        if not directory.is_dir(): raise DataNotFound("Resource not found")
        rows=[]
        for child in sorted(directory.iterdir(),key=lambda p:(not p.is_dir(),p.name.lower())):
            rel=PurePosixPath(child.relative_to(self.root(bot)).as_posix())
            if child.is_symlink() or self._sensitive(rel): continue
            if child.is_dir(): rows.append({"name":child.name,"path":rel.as_posix(),"type":"directory","size":None,"modified":child.stat().st_mtime,"editable":False,"validation":None})
            elif child.is_file() and child.suffix.lower() in VIEW_TYPES:
                registered=next((x for x in self.sources(bot) if x.path==rel.as_posix()),None)
                rows.append({"name":child.name,"path":rel.as_posix(),"type":child.suffix.lower().lstrip("."),"size":child.stat().st_size,"modified":child.stat().st_mtime,"editable":bool(registered and registered.editable and registered.type=="json"),"validation":"schema" if registered and registered.validator else ("syntax" if child.suffix.lower()==".json" else None)})
        return rows
    def read(self,bot:Bot,relative:str)->dict:
        path=self.resolve(bot,relative)
        if not path.is_file() or path.suffix.lower() not in VIEW_TYPES: raise DataNotFound("Resource not found")
        size=path.stat().st_size
        if size>self.settings.file_view_max_bytes: raise DataError("File is too large for browser viewing.")
        content=path.read_bytes()
        if b"\x00" in content: raise DataError("Binary files cannot be viewed.")
        try: text=content.decode("utf-8")
        except UnicodeDecodeError: raise DataError("File is not valid UTF-8 text.") from None
        return {"content":text,"version":content_hash(content),"size":size,"modified":path.stat().st_mtime}
    def _depth(self,value:Any,limit:int,level:int=0)->None:
        if level>limit: raise DataValidationError("JSON exceeds the maximum nesting depth.")
        if isinstance(value,dict):
            for item in value.values(): self._depth(item,limit,level+1)
        elif isinstance(value,list):
            for item in value: self._depth(item,limit,level+1)
    def validate_json(self,content:str,source:DataSource|None=None)->Any:
        try: value=json.loads(content)
        except json.JSONDecodeError as exc: raise DataValidationError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None
        self._depth(value,self.settings.json_max_depth)
        if source and source.validator:
            try:
                if isinstance(source.validator,type) and issubclass(source.validator,BaseModel): source.validator.model_validate(value)
                else: source.validator(value)
            except Exception as exc: raise DataValidationError("JSON does not match the registered schema.",{"schema":str(exc)}) from None
        return value
    def _atomic_write(self,path:Path,content:bytes,validator)->None:
        original=path.stat(); fd,name=tempfile.mkstemp(prefix=f".{path.name}.",suffix=".tmp",dir=path.parent)
        temporary=Path(name)
        try:
            with os.fdopen(fd,"wb") as stream: stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary,stat.S_IMODE(original.st_mode))
            try: os.chown(temporary,original.st_uid,original.st_gid)
            except PermissionError: pass
            validator(temporary.read_text(encoding="utf-8"))
            os.replace(temporary,path)
            # Persist the directory entry as well as the file data.
            directory_fd=os.open(path.parent,os.O_RDONLY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
            if path.read_bytes()!=content: raise DataError("Final file verification failed.")
        finally: temporary.unlink(missing_ok=True)
    def save_json(self,bot:Bot,source:DataSource,actor:User,content:str,base_version:str,event_type:EventType=EventType.JSON_DATA_CHANGED)->dict:
        if not source.editable or source.type!="json": raise DataNotFound("Resource not found")
        encoded=content.encode("utf-8")
        if len(encoded)>self.settings.file_edit_max_bytes: raise DataValidationError("File is too large for browser editing.")
        self.validate_json(content,source)
        path=self.resolve(bot,source.path)
        with bot_data_locks.acquire(bot.id):
            old=path.read_bytes(); old_hash=content_hash(old)
            if old_hash!=base_version: raise DataConflict("This file has changed since you opened it. Reload the latest version before saving.")
            try: backup=self.backups.create(bot,actor,BackupType.PRE_EDIT,f"Before editing {source.id}",protected=True)
            except Exception as exc: raise DataError("Unable to save changes because the safety backup could not be created.") from exc
            if backup.verification_status is not VerificationStatus.VERIFIED: raise DataError("Unable to save changes because the safety backup could not be verified.")
            operation=create_operation(self.db,"activity",user_id=actor.id,bot_id=bot.id,event_metadata={"action":"data.edit","data_source":source.id}); operation.status=OperationStatus.RUNNING
            try:
                self._atomic_write(path,encoded,lambda value:self.validate_json(value,source)); new_hash=content_hash(encoded)
                version=DataVersion(bot_id=bot.id,data_source=source.id,relative_path=source.path,actor_id=actor.id,operation_id=operation.public_id,backup_id=backup.id,previous_hash=old_hash,new_hash=new_hash); self.db.add(version)
                operation.status=OperationStatus.COMPLETED; operation.completed_at=utcnow(); operation.event_metadata={"data_source":source.id,"previous_hash":old_hash,"new_hash":new_hash}
                payload={"data_source":source.id,"relative_path":source.path,"operation_id":operation.public_id,"previous_hash":old_hash,"new_hash":new_hash}
                event=DomainEvent(event_type,actor,bot.id,payload); EventBus(self.db).publish(event); AuditService(self.db).record(event,"success",source.id,operation.public_id); backup.protected=False; self.db.commit()
                return {"version":new_hash,"operation_id":operation.public_id,"backup_id":backup.public_id}
            except Exception as exc:
                operation.status=OperationStatus.FAILED; operation.completed_at=utcnow(); operation.error=str(exc)[:255]; self.db.commit(); raise
    def history(self,bot:Bot,source:DataSource)->list[DataVersion]:
        return list(self.db.scalars(select(DataVersion).where(DataVersion.bot_id==bot.id,DataVersion.data_source==source.id).order_by(DataVersion.created_at.desc()).limit(100)))
    def _backup_content(self,version:DataVersion)->str:
        backup=version.backup; root=self.backups.backup_dir(backup)
        with tarfile.open(root/backup.archive_name,"r:gz") as archive:
            member=archive.getmember(version.relative_path)
            if not member.isfile() or member.issym() or member.islnk(): raise DataError("Historical version is unavailable.")
            data=archive.extractfile(member).read(self.settings.file_view_max_bytes+1)
        if len(data)>self.settings.file_view_max_bytes: raise DataError("Historical version is too large.")
        return data.decode("utf-8")
    def diff(self,bot:Bot,source:DataSource,version:DataVersion)->str:
        if version.bot_id!=bot.id or version.data_source!=source.id: raise DataNotFound("Resource not found")
        before=self._backup_content(version); current=self.read(bot,source.path)["content"]
        if source.sensitive_fields:
            before=self._redacted_json(before,source.sensitive_fields); current=self._redacted_json(current,source.sensitive_fields)
        return "\n".join(difflib.unified_diff(before.splitlines(),current.splitlines(),fromfile="previous",tofile="current",lineterm=""))
    @staticmethod
    def _redacted_json(content:str,fields:tuple[str,...])->str:
        value=json.loads(content)
        if isinstance(value,dict):
            for key in fields:
                if key in value: value[key]="[REDACTED]"
        return json.dumps(value,indent=4,ensure_ascii=False)
    def config_view(self,bot:Bot,source:DataSource)->dict:
        raw=self.read(bot,source.path); values=json.loads(raw["content"]); public={}
        for field in source.config_fields:
            value=values.get(field.key,field.default)
            public[field.key]={"configured":value is not None} if field.sensitive else value
        return {"values":public,"version":raw["version"],"modified":raw["modified"]}
    def validate_config(self,source:DataSource,submitted:dict,current:dict)->tuple[dict,bool]:
        result=dict(current); errors={}; restart=False
        for field in source.config_fields:
            if not field.editable: continue
            if field.sensitive and field.key not in submitted: continue
            value=submitted.get(field.key,field.default)
            if field.required and (value is None or value==""): errors[field.key]="This field is required."; continue
            try: value=self._config_value(field,value)
            except ValueError as exc: errors[field.key]=str(exc); continue
            if result.get(field.key)!=value: restart=restart or field.requires_restart
            result[field.key]=value
        if errors: raise DataValidationError("Configuration validation failed.",errors)
        if source.validator:
            self.validate_json(json.dumps(result),source)
        return result,restart
    def save_config(self,bot:Bot,source:DataSource,actor:User,submitted:dict,base_version:str)->dict:
        raw=self.read(bot,source.path); current=json.loads(raw["content"])
        if not isinstance(current,dict): raise DataValidationError("Registered configuration must be a JSON object.")
        result,restart=self.validate_config(source,submitted,current)
        saved=self.save_json(bot,source,actor,json.dumps(result,indent=4,ensure_ascii=False)+"\n",base_version,EventType.BOT_CONFIGURATION_CHANGED)
        saved["restart_required"]=restart
        return saved
    @staticmethod
    def _config_value(field:ConfigField,value:Any)->Any:
        kind=field.type
        if kind=="boolean":
            if not isinstance(value,bool): raise ValueError("Must be on or off.")
        elif kind=="integer":
            if isinstance(value,bool) or not isinstance(value,int): raise ValueError("Must be a whole number.")
        elif kind=="float":
            if isinstance(value,bool) or not isinstance(value,(int,float)): raise ValueError("Must be a number.")
            value=float(value)
        elif kind=="choice":
            if value not in field.choices: raise ValueError("Choose one of the available options.")
        elif kind=="colour":
            if not isinstance(value,str) or not COLOUR.fullmatch(value): raise ValueError("Use a six-digit hex colour.")
            value=value.upper()
        elif kind in {"channel_id","role_id","user_id"}:
            if not isinstance(value,str) or not SNOWFLAKE.fullmatch(value): raise ValueError("Invalid Discord ID.")
        elif kind=="duration":
            if isinstance(value,bool) or not isinstance(value,int) or value<0: raise ValueError("Duration must be a non-negative number of seconds.")
        elif kind=="string":
            if not isinstance(value,str): raise ValueError("Must be text.")
        else: raise ValueError("Unsupported configuration field type.")
        if isinstance(value,(int,float)):
            if field.minimum is not None and value<field.minimum: raise ValueError(f"Must be at least {field.minimum}.")
            if field.maximum is not None and value>field.maximum: raise ValueError(f"Must be at most {field.maximum}.")
        return value
