from __future__ import annotations

import hashlib
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.base import BotState, DatabaseColumn, DatabaseEditPolicy, DatabaseSource, DatabaseTable
from app.adapters.registry import get_adapter
from app.core.config import Settings, get_settings
from app.core.events import AuditService, DomainEvent, EventBus, EventType
from app.core.operations import create_operation
from app.models import BackupType, Bot, OperationStatus, User, VerificationStatus, utcnow
from app.services.backups import BackupService, bot_data_locks
from app.services.process_manager import BotProcessManager, ProcessConflict, process_manager, process_workflow_locks
from app.services.data import BotDataService

PLATFORM_NAMES={"platform.db","platform.sqlite","platform.sqlite3","dashboard.db","permissions.db","sessions.db","audit.db"}
OPERATORS={"equals":"=","not_equals":"!=","contains":"LIKE","starts_with":"LIKE","greater_than":">","less_than":"<","before":"<","after":">","is_null":"IS NULL","is_not_null":"IS NOT NULL"}

class SQLiteDataError(RuntimeError): pass
class SQLiteNotFound(SQLiteDataError): pass
class SQLiteValidationError(SQLiteDataError): pass
class SQLiteConflict(SQLiteDataError): pass
class SQLiteBusy(SQLiteDataError): pass
class SQLiteIntegrityError(SQLiteDataError): pass

class SQLiteDataService:
    """Structured, bot-scoped SQLite inspection and mutation; never accepts SQL."""
    def __init__(self,db:Session,settings:Settings|None=None,manager:BotProcessManager|None=None):
        self.db=db; self.settings=settings or get_settings(); self.data=BotDataService(db,self.settings); self.backups=BackupService(db,self.settings); self.manager=manager or process_manager
    def sources(self,bot:Bot)->tuple[DatabaseSource,...]: return tuple(get_adapter(bot.adapter).get_database_sources())
    def source(self,bot:Bot,source_id:str)->tuple[DatabaseSource,Path]:
        source=next((s for s in self.sources(bot) if s.id==source_id),None)
        if not source: raise SQLiteNotFound("Resource not found")
        try: path=self.data.resolve(bot,source.path)
        except Exception: raise SQLiteNotFound("Resource not found") from None
        if path.name.lower() in PLATFORM_NAMES or self._is_platform_path(path) or not path.is_file(): raise SQLiteNotFound("Resource not found")
        return source,path
    def _is_platform_path(self,path:Path)->bool:
        url=self.settings.database_url
        if not url.startswith("sqlite:///"): return False
        configured=Path(url.removeprefix("sqlite:///")).resolve()
        return path.resolve()==configured
    @staticmethod
    def _connect(path:Path,readonly:bool=True):
        try:
            if readonly: connection=sqlite3.connect(f"file:{path.as_posix()}?mode=ro",uri=True,timeout=2)
            else: connection=sqlite3.connect(path,timeout=2,isolation_level=None)
            connection.row_factory=sqlite3.Row; connection.execute("PRAGMA busy_timeout=2000"); connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.Error as exc: raise SQLiteDataError("Database is unavailable.") from exc
    @staticmethod
    def _quote(identifier:str)->str: return '"'+identifier.replace('"','""')+'"'
    def _table(self,source:DatabaseSource,path:Path,name:str)->DatabaseTable:
        if name.startswith("sqlite_"): raise SQLiteNotFound("Resource not found")
        registered=next((t for t in source.tables if t.name==name),None)
        if source.tables:
            if not registered or not registered.visible: raise SQLiteNotFound("Resource not found")
            return registered
        with closing(self._connect(path)) as con:
            found=con.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=? AND name NOT LIKE 'sqlite_%'",(name,)).fetchone()
        if not found: raise SQLiteNotFound("Resource not found")
        return DatabaseTable(name=name)
    def _schema(self,path:Path,table:DatabaseTable)->list[dict]:
        configured={c.key:c for c in table.columns}
        with closing(self._connect(path)) as con:
            rows=con.execute(f"PRAGMA table_info({self._quote(table.name)})").fetchall()
            foreign={r[3]:(r[2],r[4]) for r in con.execute(f"PRAGMA foreign_key_list({self._quote(table.name)})")}
        if not rows: raise SQLiteNotFound("Resource not found")
        result=[]
        for row in rows:
            cfg=configured.get(row[1]); hidden=bool(cfg and (cfg.hidden or cfg.sensitive))
            result.append({"name":row[1],"label":cfg.label if cfg and cfg.label else row[1].replace("_"," ").title(),"type":(cfg.type if cfg and cfg.type else row[2] or "ANY").upper(),"nullable":not bool(row[3]),"primary_key":bool(row[5]),"default":row[4],"hidden":hidden,"sensitive":bool(cfg and cfg.sensitive),"editable":bool(table.editable and cfg and cfg.editable and not hidden and not row[5]),"choices":list(cfg.choices) if cfg else [],"foreign_key":foreign.get(row[1])})
        return result
    def overview(self,bot:Bot)->list[dict]:
        result=[]
        for source in self.sources(bot):
            try:
                _,path=self.source(bot,source.id); health=self.health(path)
                result.append({"id":source.id,"label":source.label,"editable":source.editable,"size":path.stat().st_size,"modified":path.stat().st_mtime,**health})
            except SQLiteDataError: continue
        return result
    def health(self,path:Path)->dict:
        try:
            with closing(self._connect(path)) as con:
                status=con.execute("PRAGMA quick_check").fetchone()[0]; version=con.execute("SELECT sqlite_version()").fetchone()[0]; journal=con.execute("PRAGMA journal_mode").fetchone()[0]
            return {"integrity":"healthy" if status=="ok" else "failed","sqlite_version":version,"journal_mode":journal}
        except sqlite3.Error: return {"integrity":"failed","sqlite_version":sqlite3.sqlite_version,"journal_mode":"unknown"}
    def tables(self,bot:Bot,source_id:str)->list[dict]:
        source,path=self.source(bot,source_id)
        with closing(self._connect(path)) as con:
            names=[r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 500")]
        allowed=[self._table(source,path,n) for n in names if not source.tables or any(t.name==n and t.visible for t in source.tables)]
        return [{"name":t.name,"label":t.label or t.name,"editable":source.editable and t.editable,"allow_insert":source.editable and t.allow_insert,"allow_delete":source.editable and t.allow_delete} for t in allowed]
    def schema(self,bot:Bot,source_id:str,table_name:str)->list[dict]:
        source,path=self.source(bot,source_id); return self._schema(path,self._table(source,path,table_name))
    def browse(self,bot:Bot,source_id:str,table_name:str,page:int=1,page_size:int=50,sort:str|None=None,direction:str="asc",search:str|None=None,filters:list[dict]|None=None)->dict:
        source,path=self.source(bot,source_id); table=self._table(source,path,table_name); schema=self._schema(path,table)
        visible=[c for c in schema if not c["hidden"]]; names={c["name"] for c in visible}
        if page<1 or page_size not in {25,50,100}: raise SQLiteValidationError("Invalid pagination request.")
        if sort is not None and sort not in names: raise SQLiteValidationError("Invalid sort column.")
        if direction not in {"asc","desc"}: raise SQLiteValidationError("Invalid sort direction.")
        where,params=self._where(visible,table,search,filters or [])
        projection=",".join(self._quote(c["name"]) for c in visible)
        order=self._quote(sort) if sort else next((self._quote(c["name"]) for c in schema if c["primary_key"]),"rowid")
        try:
            with closing(self._connect(path)) as con:
                total=con.execute(f"SELECT COUNT(*) FROM {self._quote(table.name)}{where}",params).fetchone()[0]
                rows=con.execute(f"SELECT {projection} FROM {self._quote(table.name)}{where} ORDER BY {order} {direction.upper()} LIMIT ? OFFSET ?",(*params,page_size,(page-1)*page_size)).fetchall()
        except sqlite3.OperationalError as exc: self._sqlite_error(exc)
        return {"page":page,"page_size":page_size,"total":total,"columns":visible,"rows":[self._public_row(dict(r),visible) for r in rows]}
    def _where(self,visible:list[dict],table:DatabaseTable,search:str|None,filters:list[dict])->tuple[str,list[Any]]:
        columns={c["name"]:c for c in visible}; clauses=[]; params=[]
        if search:
            if len(search)>200: raise SQLiteValidationError("Search is too long.")
            configured=table.search_columns or tuple(c["name"] for c in visible if any(x in c["type"] for x in ("TEXT","CHAR","CLOB")))[:5]
            searched=[x for x in configured if x in columns]
            if searched: clauses.append("("+" OR ".join(f"{self._quote(x)} LIKE ? ESCAPE '\\'" for x in searched)+")"); params.extend([f"%{self._like(search)}%"]*len(searched))
        for item in filters[:10]:
            name=item.get("column"); op=item.get("operator")
            if name not in columns or op not in OPERATORS: raise SQLiteValidationError("Invalid filter.")
            sqlop=OPERATORS[op]; clauses.append(f"{self._quote(name)} {sqlop}" + ("" if op.startswith("is_") else " ?"))
            if not op.startswith("is_"):
                value=item.get("value")
                if op=="contains": value=f"%{self._like(str(value))}%"
                elif op=="starts_with": value=f"{self._like(str(value))}%"
                params.append(value)
        return (" WHERE "+" AND ".join(clauses) if clauses else ""),params
    @staticmethod
    def _like(value:str)->str: return value.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
    @staticmethod
    def _public_row(row:dict,schema:list[dict])->dict:
        out={}
        for column in schema:
            value=row.get(column["name"])
            if isinstance(value,bytes): value={"type":"BLOB","size":len(value)}
            elif isinstance(value,str) and len(value)>4096: value=value[:4096]+"…"
            out[column["name"]]=value
        return out
    def row(self,bot:Bot,source_id:str,table_name:str,key:dict[str,Any])->dict:
        source,path=self.source(bot,source_id); table=self._table(source,path,table_name); schema=self._schema(path,table)
        row=self._current(path,table,schema,key); public=self._public_row({k:v for k,v in row.items() if not next(c for c in schema if c["name"]==k)["hidden"]},[c for c in schema if not c["hidden"]])
        return {"row":public,"key":key,"concurrency_token":self._token(row,schema),"columns":[c for c in schema if not c["hidden"]]}
    def _current(self,path,table,schema,key):
        pk=[c["name"] for c in schema if c["primary_key"]]
        if not pk or set(key)!=set(pk): raise SQLiteValidationError("A complete primary key is required.")
        where=" AND ".join(f"{self._quote(k)}=?" for k in pk)
        with closing(self._connect(path)) as con: row=con.execute(f"SELECT * FROM {self._quote(table.name)} WHERE {where} LIMIT 1",tuple(key[k] for k in pk)).fetchone()
        if not row: raise SQLiteNotFound("Resource not found")
        return dict(row)
    @staticmethod
    def _token(row,schema):
        state={c["name"]:row.get(c["name"]) for c in schema if not c["sensitive"]}
        normalized=json.dumps(state,sort_keys=True,separators=(",",":"),default=lambda x:{"blob_sha256":hashlib.sha256(x).hexdigest() if isinstance(x,bytes) else str(x)})
        return hashlib.sha256(normalized.encode()).hexdigest()
    def _validate_values(self,table,schema,values,creating=False):
        configs={c.key:c for c in table.columns}
        allowed={c["name"]:c for c in schema if c["editable"] or (creating and not c["primary_key"] and not c["hidden"] and bool(configs.get(c["name"]) and configs[c["name"]].editable))}
        if not values or any(k not in allowed for k in values): raise SQLiteValidationError("One or more fields cannot be changed.")
        if creating:
            missing=[c["label"] for c in schema if c["name"] in allowed and not c["nullable"] and c["default"] is None and c["name"] not in values]
            if missing: raise SQLiteValidationError(f"Required fields are missing: {', '.join(missing)}.")
        result={}
        for key,value in values.items():
            column=allowed[key]; cfg=configs.get(key)
            if value is None:
                if not column["nullable"]: raise SQLiteValidationError(f"{column['label']} is required.")
            elif cfg and cfg.choices and value not in cfg.choices: raise SQLiteValidationError(f"Invalid value for {column['label']}.")
            elif "INT" in column["type"] and (isinstance(value,bool) or not isinstance(value,int)): raise SQLiteValidationError(f"{column['label']} must be a whole number.")
            elif any(x in column["type"] for x in ("REAL","FLOA","DOUB")) and (isinstance(value,bool) or not isinstance(value,(int,float))): raise SQLiteValidationError(f"{column['label']} must be numeric.")
            elif "BLOB" in column["type"]: raise SQLiteValidationError("BLOB editing is not supported.")
            if cfg and isinstance(value,(int,float)) and ((cfg.minimum is not None and value<cfg.minimum) or (cfg.maximum is not None and value>cfg.maximum)): raise SQLiteValidationError(f"{column['label']} is outside the allowed range.")
            if cfg and cfg.validator:
                try: value=cfg.validator(value)
                except Exception as exc: raise SQLiteValidationError(f"Invalid value for {column['label']}.") from exc
            result[key]=value
        return result
    def mutate(self,bot,source_id,table_name,actor,action,values=None,key=None,token=None,confirmation=None):
        source,path=self.source(bot,source_id); table=self._table(source,path,table_name); schema=self._schema(path,table)
        if not source.editable or not table.editable or source.mutation_policy is not DatabaseEditPolicy.LIVE_EDIT_SUPPORTED: raise SQLiteNotFound("Resource not found")
        if action=="create" and not table.allow_insert: raise SQLiteNotFound("Resource not found")
        if action=="delete" and (not table.allow_delete or confirmation!=f"DELETE {table.name}"): raise SQLiteValidationError("Deletion confirmation did not match.")
        clean={} if action=="delete" else self._validate_values(table,schema,values or {},action=="create")
        with bot_data_locks.acquire(bot.id):
            if self.health(path)["integrity"]!="healthy": raise SQLiteIntegrityError("Database integrity validation failed. Editing has been blocked.")
            current=None
            if action!="create":
                current=self._current(path,table,schema,key or {})
                if self._token(current,schema)!=token: raise SQLiteConflict("This record changed since you opened it.")
            try: backup=self.backups.create(bot,actor,BackupType.PRE_EDIT,f"Before {action} in {source.id}.{table.name}",protected=True)
            except Exception as exc: raise SQLiteDataError("Unable to change the database because the safety backup could not be created.") from exc
            if backup.verification_status is not VerificationStatus.VERIFIED: raise SQLiteDataError("Unable to change the database because the safety backup could not be verified.")
            operation=create_operation(self.db,"activity",user_id=actor.id,bot_id=bot.id,event_metadata={"action":f"database.{action}","database":source.id,"table":table.name}); operation.status=OperationStatus.RUNNING
            try:
                identifier,newrow=self._transaction(path,table,schema,action,clean,key or {},token)
                self._post_write_validate(source,path)
                changes={k:{"before":current.get(k) if current else None,"after":newrow.get(k) if newrow else None} for k in clean if not next(c for c in schema if c["name"]==k)["sensitive"]}
                et={"create":EventType.DATABASE_ROW_CREATED,"update":EventType.DATABASE_ROW_UPDATED,"delete":EventType.DATABASE_ROW_DELETED}[action]
                payload={"database_id":source.id,"table":table.name,"record":identifier,"operation_id":operation.public_id,"changes":changes}
                event=DomainEvent(et,actor,bot.id,payload); EventBus(self.db).publish(event); AuditService(self.db).record(event,"success",f"{source.id}.{table.name}:{identifier}",operation.public_id); EventBus(self.db).publish(DomainEvent(EventType.BOT_DATABASE_CHANGED,actor,bot.id,{k:v for k,v in payload.items() if k!="changes"}))
                operation.status=OperationStatus.COMPLETED; operation.completed_at=utcnow(); backup.protected=False; self.db.commit()
                return {"operation_id":operation.public_id,"backup_id":backup.public_id,"record":identifier,"concurrency_token":self._token(newrow,schema) if newrow else None}
            except Exception as exc:
                operation.status=OperationStatus.FAILED; operation.completed_at=utcnow(); operation.error=str(exc)[:255]; self.db.commit(); raise
    def _transaction(self,path,table,schema,action,values,key,token=None):
        pk=[c["name"] for c in schema if c["primary_key"]]
        con=self._connect(path,False)
        try:
            con.execute("BEGIN IMMEDIATE")
            if action!="create":
                where=" AND ".join(f"{self._quote(k)}=?" for k in pk); args=tuple(key[k] for k in pk)
                locked=con.execute(f"SELECT * FROM {self._quote(table.name)} WHERE {where} LIMIT 1",args).fetchone()
                if not locked: raise SQLiteConflict("This record is no longer available.")
                if token is not None and self._token(dict(locked),schema)!=token: raise SQLiteConflict("This record changed before the write began.")
            if action=="create":
                names=list(values); sql=f"INSERT INTO {self._quote(table.name)} ({','.join(self._quote(x) for x in names)}) VALUES ({','.join('?' for _ in names)})"; cur=con.execute(sql,tuple(values[x] for x in names)); identifier=str(cur.lastrowid); lookup={pk[0]:cur.lastrowid} if len(pk)==1 else {}
            else:
                where=" AND ".join(f"{self._quote(k)}=?" for k in pk); args=tuple(key[k] for k in pk); identifier="/".join(str(key[k]) for k in pk); lookup=key
                if action=="update":
                    cur=con.execute(f"UPDATE {self._quote(table.name)} SET {','.join(f'{self._quote(k)}=?' for k in values)} WHERE {where}",(*values.values(),*args))
                else: cur=con.execute(f"DELETE FROM {self._quote(table.name)} WHERE {where}",args)
                if cur.rowcount!=1: raise SQLiteConflict("This record is no longer available.")
            newrow=None
            if action!="delete" and lookup:
                where=" AND ".join(f"{self._quote(k)}=?" for k in lookup); newrow=dict(con.execute(f"SELECT * FROM {self._quote(table.name)} WHERE {where}",tuple(lookup.values())).fetchone())
            check=con.execute("PRAGMA quick_check").fetchone()[0]
            if check!="ok": raise SQLiteIntegrityError("Database integrity validation failed.")
            con.commit(); return identifier,newrow
        except sqlite3.IntegrityError as exc: con.rollback(); raise SQLiteValidationError("The change violates a database constraint.") from exc
        except sqlite3.OperationalError as exc: con.rollback(); self._sqlite_error(exc)
        except Exception: con.rollback(); raise
        finally: con.close()

    def _post_write_validate(self,source:DatabaseSource,path:Path)->None:
        if self.health(path)["integrity"]!="healthy": raise SQLiteIntegrityError("Post-write integrity validation failed.")
        if source.validator:
            try: result=source.validator(str(path))
            except Exception as exc: raise SQLiteIntegrityError("Database domain validation failed.") from exc
            if result is False: raise SQLiteIntegrityError("Database domain validation failed.")

    def _workflow_event(self,event_type,actor,bot,operation,result="success",**payload):
        event=DomainEvent(event_type,actor,bot.id,{"operation_id":operation.public_id,**payload})
        EventBus(self.db).publish(event); AuditService(self.db).record(event,result,bot.id,operation.public_id)

    def _stage(self,operation,stage,**facts):
        metadata=dict(operation.event_metadata or {}); metadata.update({"stage":stage,**facts}); operation.event_metadata=metadata
        self.db.commit()

    async def _wait_for(self,bot,predicate,timeout):
        deadline=asyncio.get_running_loop().time()+timeout; latest=None
        while asyncio.get_running_loop().time()<deadline:
            latest=await self.manager.get_status(bot.id,bot.enabled)
            if predicate(latest): return latest
            await asyncio.sleep(min(.1,timeout/10))
        return latest or await self.manager.get_status(bot.id,bot.enabled)

    async def mutate_process_aware(self,bot,source_id,table_name,actor,action,values=None,key=None,token=None,confirmation=None):
        """Run the durable, supervisor-mediated offline edit workflow."""
        source,path=self.source(bot,source_id); table=self._table(source,path,table_name); schema=self._schema(path,table)
        if source.mutation_policy is DatabaseEditPolicy.LIVE_EDIT_SUPPORTED:
            result=self.mutate(bot,source_id,table_name,actor,action,values,key,token,confirmation)
            return {**result,"database_change":"success","integrity":"valid","bot_restart":"not_required","discord_ready":"not_required"}
        if not source.editable or not table.editable: raise SQLiteNotFound("Resource not found")
        if action=="create" and not table.allow_insert: raise SQLiteNotFound("Resource not found")
        if action=="delete" and (not table.allow_delete or confirmation!=f"DELETE {table.name}"): raise SQLiteValidationError("Deletion confirmation did not match.")
        clean={} if action=="delete" else self._validate_values(table,schema,values or {},action=="create")
        if action!="create":
            current=self._current(path,table,schema,key or {})
            if self._token(current,schema)!=token: raise SQLiteConflict("This record changed since you opened it.")
        else: current=None
        operation=create_operation(self.db,"activity",user_id=actor.id,bot_id=bot.id,event_metadata={"action":f"database.{action}","database":source.id,"table":table.name,"stage":"authorised"})
        operation.status=OperationStatus.RUNNING; self._workflow_event(EventType.DATABASE_EDIT_STARTED,actor,bot,operation,result="requested",database=source.id,table=table.name); self.db.commit()
        backup=None; stopped=False; initially_running=False; old_instance=None; committed=False
        result={"operation_id":operation.public_id,"database_change":"not_started","integrity":"not_checked","rollback":"not_required","bot_restart":"not_required","discord_ready":"not_required"}
        try:
            with process_workflow_locks.acquire(bot.id), bot_data_locks.acquire(bot.id):
                self._stage(operation,"backup")
                try: backup=self.backups.create(bot,actor,BackupType.PRE_EDIT,f"Before {action} in {source.id}.{table.name}",protected=True)
                except Exception as exc: raise SQLiteDataError("The database change was not applied because the required safety backup could not be verified.") from exc
                if backup.verification_status is not VerificationStatus.VERIFIED or not self.backups.verify(backup):
                    raise SQLiteDataError("The database change was not applied because the required safety backup could not be verified.")
                result["backup_id"]=backup.public_id; self._stage(operation,"stopping",backup_id=backup.public_id)
                before=await self.manager.get_status(bot.id,bot.enabled); initially_running=before.process_running; old_instance=before.instance_id
                if initially_running:
                    await self.manager.stop_bot(bot)
                    offline=await self._wait_for(bot,lambda h:not h.process_running and h.state is BotState.OFFLINE,self.settings.supervisor_stop_timeout)
                    if offline.process_running or offline.state is not BotState.OFFLINE: raise SQLiteDataError(f"Bot stop could not be confirmed; current state is {offline.state.value}.")
                    stopped=True
                self._stage(operation,"transaction",old_instance_id=old_instance)
                # Re-resolve the registration/path/schema after the process is offline.
                source,path=self.source(bot,source_id); table=self._table(source,path,table_name); schema=self._schema(path,table)
                identifier,newrow=self._transaction(path,table,schema,action,clean,key or {},token); committed=True; result["database_change"]="success"; result["record"]=identifier
                self._workflow_event(EventType.DATABASE_EDIT_APPLIED,actor,bot,operation,database=source.id,table=table.name,record=identifier,backup_id=backup.public_id)
                self._stage(operation,"validation")
                try: self._post_write_validate(source,path); result["integrity"]="valid"; self._workflow_event(EventType.DATABASE_EDIT_VALIDATED,actor,bot,operation,backup_id=backup.public_id)
                except Exception:
                    result["database_change"]="failed"; result["integrity"]="invalid"; self._workflow_event(EventType.DATABASE_VALIDATION_FAILED,actor,bot,operation,"failed",backup_id=backup.public_id)
                    self._workflow_event(EventType.DATABASE_EDIT_RECOVERY_STARTED,actor,bot,operation,result="requested",backup_id=backup.public_id)
                    try:
                        self.backups.recover_pre_edit(bot,backup,actor,operation,lock_already_held=True); source,path=self.source(bot,source_id); self._post_write_validate(source,path)
                        result["rollback"]="success"; result["integrity"]="restored_valid"; self._workflow_event(EventType.DATABASE_EDIT_ROLLED_BACK,actor,bot,operation,backup_id=backup.public_id)
                    except Exception as recovery:
                        result["rollback"]="failed"; self._workflow_event(EventType.DATABASE_EDIT_RECOVERY_FAILED,actor,bot,operation,"failed",backup_id=backup.public_id)
                        raise SQLiteIntegrityError("The database change failed and automatic recovery could not be validated. The bot was not restarted.") from recovery
                if result["database_change"]=="success":
                    row_event={"create":EventType.DATABASE_ROW_CREATED,"update":EventType.DATABASE_ROW_UPDATED,"delete":EventType.DATABASE_ROW_DELETED}[action]
                    payload={"database_id":source.id,"table":table.name,"record":identifier,"operation_id":operation.public_id,"backup_id":backup.public_id,"old_instance_id":old_instance}
                    event=DomainEvent(row_event,actor,bot.id,payload); EventBus(self.db).publish(event); AuditService(self.db).record(event,"success",f"{source.id}.{table.name}:{identifier}",operation.public_id)
                    EventBus(self.db).publish(DomainEvent(EventType.BOT_DATABASE_CHANGED,actor,bot.id,payload))
                if initially_running:
                    self._stage(operation,"restart")
                    try: started_health=await self.manager.start_bot(bot)
                    except Exception as exc:
                        result["bot_restart"]="failed"; self._workflow_event(EventType.DATABASE_EDIT_RESTART_FAILED,actor,bot,operation,"failed",database_change=result["database_change"]); raise SQLiteDataError("Database processing completed, but the bot could not be restarted.") from exc
                    new_instance=started_health.instance_id; healthy=await self._wait_for(bot,lambda h:h.process_running and h.instance_id==new_instance,min(self.settings.bot_heartbeat_timeout_seconds,self.settings.bot_ready_timeout_seconds))
                    if not healthy.process_running:
                        result["bot_restart"]="startup_crash"; self._workflow_event(EventType.BOT_PROCESS_FAILED,actor,bot,operation,"failed",stage="startup",instance_id=new_instance); raise SQLiteDataError("Database processing completed, but the bot process exited during startup.")
                    result["bot_restart"]="success"; result["new_instance_id"]=new_instance; self._workflow_event(EventType.DATABASE_EDIT_BOT_RESTARTED,actor,bot,operation,old_instance_id=old_instance,new_instance_id=new_instance)
                    self._stage(operation,"ready_check")
                    ready=await self._wait_for(bot,lambda h:h.process_running and h.instance_id==new_instance and h.discord_connected and h.discord_ready and h.heartbeat_fresh,self.settings.bot_ready_timeout_seconds)
                    if ready.process_running and ready.instance_id==new_instance and ready.discord_connected and ready.discord_ready and ready.heartbeat_fresh:
                        result["discord_ready"]="success"; self._workflow_event(EventType.DATABASE_EDIT_READY_CONFIRMED,actor,bot,operation,instance_id=new_instance)
                    else:
                        result["discord_ready"]="timeout" if ready.process_running else "startup_crash"; et=EventType.DATABASE_EDIT_READY_TIMEOUT if ready.process_running else EventType.BOT_PROCESS_FAILED
                        self._workflow_event(et,actor,bot,operation,"failed",instance_id=new_instance,process_state=ready.state.value,timeout=self.settings.bot_ready_timeout_seconds)
                operation.status=OperationStatus.COMPLETED if result["database_change"]=="success" else OperationStatus.FAILED; operation.completed_at=utcnow(); operation.event_metadata={**operation.event_metadata,**result,"stage":"complete"}; backup.protected=False; self.db.commit(); return result
        except Exception as exc:
            # A pre-commit failure leaves SQLite unchanged, but an online bot that
            # was stopped must still be recovered through the supervisor.
            if stopped and not committed:
                try:
                    restarted=await self.manager.start_bot(bot); result["bot_restart"]="success" if restarted.process_running else "failed"
                    if restarted.process_running:
                        ready=await self._wait_for(bot,lambda h:h.process_running and h.instance_id==restarted.instance_id and h.discord_connected and h.discord_ready and h.heartbeat_fresh,self.settings.bot_ready_timeout_seconds)
                        result["discord_ready"]="success" if ready.discord_ready and ready.heartbeat_fresh else "timeout"
                except Exception: result["bot_restart"]="failed"
            result["database_change"]="success" if committed and result["database_change"]=="success" else "failed"
            operation.status=OperationStatus.FAILED; operation.completed_at=utcnow(); operation.error=str(exc)[:255]; operation.event_metadata={**operation.event_metadata,**result,"stage":"failed"}
            self._workflow_event(EventType.DATABASE_EDIT_FAILED,actor,bot,operation,"failed",stage=operation.event_metadata.get("stage"),database_change=result["database_change"],bot_restart=result["bot_restart"],discord_ready=result["discord_ready"]); self.db.commit()
            exc.workflow_result=result
            raise
    @staticmethod
    def _sqlite_error(exc):
        if "locked" in str(exc).lower() or "busy" in str(exc).lower(): raise SQLiteBusy("Database is temporarily busy. Please try again.") from exc
        raise SQLiteDataError("The database operation could not be completed.") from exc
