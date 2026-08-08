from __future__ import annotations

import os
import subprocess
import threading
import uuid
import secrets
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActivityEvent, Bot, BotInstance, utcnow
from app.core.config import get_settings
from app.services.bot_state import BotStateResolver, StateInputs


class SupervisorConflict(RuntimeError): pass
class IdentityMismatch(RuntimeError): pass
class BotNotRegistered(LookupError): pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None: return value
    return value.replace(tzinfo=timezone.utc)


class SupervisorService:
    """Durable registry and conservative OS-process reconciler.

    Only bot IDs enter this boundary. Launch details always come from the trusted
    database registry, and persisted PID metadata is never sufficient by itself.
    """
    def __init__(self, db_factory, stop_timeout: float = 10.0, console_capture=None):
        self.db_factory=db_factory; self.stop_timeout=stop_timeout; self.console_capture=console_capture; self.instance_id=f"SUP-{uuid.uuid4()}"
        self._locks: dict[str, threading.Lock]={}; self._guard=threading.Lock()

    def _lock(self, bot_id: str) -> threading.Lock:
        with self._guard: return self._locks.setdefault(bot_id,threading.Lock())

    @staticmethod
    def _configuration(bot: Bot) -> tuple[str,str,str]:
        cwd=Path(bot.folder).resolve(strict=True); executable=Path(bot.python_executable).resolve(strict=True)
        entry=(cwd/bot.entry_file).resolve(strict=True)
        if cwd not in entry.parents or not entry.is_file() or not executable.is_file(): raise BotNotRegistered("Registered process configuration is unavailable")
        return str(executable),str(entry),str(cwd)

    @staticmethod
    def _identity_valid(row: BotInstance) -> bool:
        if row.pid is None or row.process_created_at is None: return False
        try:
            process=psutil.Process(row.pid)
            created=datetime.fromtimestamp(process.create_time(),timezone.utc)
            if abs((created-_aware(row.process_created_at)).total_seconds()) > 0.02: return False
            if Path(process.exe()).resolve() != Path(row.python_executable).resolve(): return False
            if Path(process.cwd()).resolve() != Path(row.working_directory).resolve(): return False
            args=process.cmdline()
            if len(args)<2: return False
            supplied=Path(args[1]); supplied=supplied if supplied.is_absolute() else Path(row.working_directory)/supplied
            return supplied.resolve() == Path(row.entry_file).resolve()
        except (psutil.Error,OSError,ValueError): return False

    @staticmethod
    def _current(db: Session, bot_id: str) -> BotInstance | None:
        return db.scalar(select(BotInstance).where(BotInstance.bot_id==bot_id).order_by(BotInstance.id.desc()).limit(1))

    @staticmethod
    def _event(db: Session, event_type: str, row: BotInstance, **payload):
        db.add(ActivityEvent(event_type=event_type,actor_id=None,bot_id=row.bot_id,payload={"source":"SYSTEM","instance_id":row.instance_id,**payload}))

    def _reconcile_row(self,db: Session,row: BotInstance) -> BotInstance:
        if self._identity_valid(row):
            previous=row.state; row.state="running"
            if previous not in {"running","starting","restarting"}: self._event(db,"BOT_PROCESS_ADOPTED",row,pid=row.pid,previous_state=previous)
            last=_aware(row.last_heartbeat_at)
            if last and (datetime.now(timezone.utc)-last).total_seconds()>get_settings().bot_heartbeat_timeout_seconds and (row.discord_connected or row.discord_ready):
                row.discord_connected=False; row.discord_ready=False; row.discord_latency_ms=None; row.guild_count=None
                self._event(db,"BOT_HEARTBEAT_LOST",row,last_heartbeat_at=last.isoformat())
        elif row.state in {"running","starting","stopping","restarting"} or row.expected_running:
            previous=row.state; row.state="crashed" if row.expected_running else "offline"; row.ended_at=row.ended_at or utcnow()
            self._event(db,"BOT_CRASHED" if row.expected_running else "BOT_PROCESS_LOST",row,pid=row.pid,previous_state=previous,state=row.state)
            row.discord_connected=False; row.discord_ready=False; row.discord_latency_ms=None; row.guild_count=None
        return row

    @staticmethod
    def _payload(row: BotInstance | None, enabled: bool=True) -> dict:
        if not enabled: return {"state":"disabled","process_running":False}
        if not row: return {"state":"offline","process_running":False}
        started=_aware(row.started_at); uptime=max(0,(datetime.now(timezone.utc)-started).total_seconds()) if row.state=="running" and started else None
        settings=get_settings(); running=row.state in {"running","starting"}
        resolved=BotStateResolver(settings.bot_heartbeat_timeout_seconds,settings.bot_ready_timeout_seconds).resolve(StateInputs(enabled,row.state,running,row.expected_running,started_at=started,connected=row.discord_connected,ready=row.discord_ready,last_heartbeat_at=row.last_heartbeat_at))
        last=row.last_heartbeat_at; fresh=bool(last and (datetime.now(timezone.utc)-_aware(last)).total_seconds()<=settings.bot_heartbeat_timeout_seconds)
        return {"state":resolved.value,"process_state":row.state,"process_running":running,"pid":row.pid,"instance_id":row.instance_id,"started_at":started.isoformat() if started else None,"uptime_seconds":uptime,"expected_running":row.expected_running,"exit_code":row.exit_code,"discord_connected":row.discord_connected if fresh else False,"discord_ready":row.discord_ready if fresh else False,"heartbeat_fresh":fresh,"last_heartbeat_at":_aware(last).isoformat() if last else None,"ready_at":_aware(row.ready_at).isoformat() if row.ready_at else None,"last_ready_at":_aware(row.last_ready_at).isoformat() if row.last_ready_at else None,"latency_ms":row.discord_latency_ms if fresh else None,"guild_count":row.guild_count if fresh else None}

    def status(self,bot_id: str) -> dict:
        with self.db_factory() as db:
            bot=db.get(Bot,bot_id)
            if not bot: raise BotNotRegistered("Bot is not registered")
            row=self._current(db,bot_id)
            if row: self._reconcile_row(db,row); db.commit()
            return self._payload(row,bot.enabled)

    def registered_instance(self,bot_id:str)->dict:
        """Read durable telemetry routing identity without triggering OS sampling."""
        with self.db_factory() as db:
            bot=db.get(Bot,bot_id)
            if not bot: raise BotNotRegistered("Bot is not registered")
            row=self._current(db,bot_id)
            return {"enabled":bot.enabled,"instance_id":row.instance_id if row else None,"process_expected":bool(row and row.state in {"running","starting"})}

    def start(self,bot_id: str) -> dict:
        if not self._lock(bot_id).acquire(blocking=False): raise SupervisorConflict("Another process operation is already in progress for this bot.")
        try:
            with self.db_factory() as db:
                bot=db.get(Bot,bot_id)
                if not bot: raise BotNotRegistered("Bot is not registered")
                if not bot.enabled: raise SupervisorConflict("Bot registration is disabled")
                current=self._current(db,bot_id)
                if current:
                    self._reconcile_row(db,current)
                    if current.state in {"running","starting","restarting","stopping"}: db.commit(); raise SupervisorConflict("Bot is already running.")
                    current.expected_running=False
                executable,entry,cwd=self._configuration(bot); instance_id=f"INST-{uuid.uuid4()}"
                management_secret=secrets.token_urlsafe(48); bot.management_secret_hash=hashlib.sha256(management_secret.encode()).hexdigest()
                row=BotInstance(bot_id=bot.id,instance_id=instance_id,state="starting",expected_running=True,python_executable=executable,entry_file=entry,working_directory=cwd,supervisor_instance_id=self.instance_id)
                db.add(row); db.commit()
                environment={k:v for k,v in os.environ.items() if k in {"PATH","HOME","USER","LOGNAME","LANG","LC_ALL","TMPDIR","TEMP","TMP","SYSTEMROOT","WINDIR"}}
                environment["BOT_INSTANCE_ID"]=instance_id
                environment.update({"BOT_MANAGEMENT_BOT_ID":bot.id,"BOT_MANAGEMENT_SECRET":management_secret,"BOT_MANAGEMENT_HEARTBEAT_URL":f"http://{get_settings().supervisor_host}:{get_settings().supervisor_port}/internal/agent/heartbeat","BOT_HEARTBEAT_INTERVAL_SECONDS":str(get_settings().bot_heartbeat_interval_seconds)})
                flags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0
                try:
                    process=subprocess.Popen([executable,entry],cwd=cwd,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE if self.console_capture else subprocess.DEVNULL,stderr=subprocess.PIPE if self.console_capture else subprocess.DEVNULL,bufsize=0,start_new_session=os.name!="nt",creationflags=flags)
                    observed=psutil.Process(process.pid); created=datetime.fromtimestamp(observed.create_time(),timezone.utc)
                    row.pid=process.pid; row.process_created_at=created; row.started_at=created; row.state="running"; db.commit()
                    if self.console_capture: self.console_capture.redactor.add_secrets((management_secret,)); self.console_capture.attach(process,bot.id,instance_id)
                    return self._payload(row)
                except (OSError,psutil.Error) as exc:
                    row.state="crashed"; row.expected_running=True; row.ended_at=utcnow(); db.commit(); raise SupervisorConflict("Bot process could not be launched") from exc
        finally: self._lock(bot_id).release()

    def stop(self,bot_id: str) -> dict:
        if not self._lock(bot_id).acquire(blocking=False): raise SupervisorConflict("Another process operation is already in progress for this bot.")
        try:
            with self.db_factory() as db:
                bot=db.get(Bot,bot_id)
                if not bot: raise BotNotRegistered("Bot is not registered")
                row=self._current(db,bot_id)
                if not row: raise SupervisorConflict("Bot process is not running.")
                self._reconcile_row(db,row)
                if row.state!="running":
                    row.expected_running=False; row.state="offline"; db.commit(); return self._payload(row)
                if not self._identity_valid(row): row.state="unknown"; db.commit(); raise IdentityMismatch("Bot process identity could not be verified.")
                row.state="stopping"; row.expected_running=False; db.commit(); process=psutil.Process(row.pid); process.terminate()
                try: code=process.wait(timeout=self.stop_timeout)
                except psutil.TimeoutExpired: process.kill(); code=process.wait(timeout=self.stop_timeout)
                row.exit_code=code; row.ended_at=utcnow(); row.state="offline"; db.commit()
                if self.console_capture: self.console_capture.ended(bot.id,row.instance_id)
                return self._payload(row)
        finally: self._lock(bot_id).release()

    def restart(self,bot_id: str) -> dict:
        if not self._lock(bot_id).acquire(blocking=False): raise SupervisorConflict("Another process operation is already in progress for this bot.")
        try:
            # Keep one lock across both phases; internal helpers deliberately bypass lock acquisition.
            with self.db_factory() as db:
                bot=db.get(Bot,bot_id); row=self._current(db,bot_id)
                if not bot or not row: raise SupervisorConflict("Bot process is not running.")
                self._reconcile_row(db,row)
                if row.state!="running" or not self._identity_valid(row): raise IdentityMismatch("Bot process identity could not be verified.")
                row.state="restarting"; row.expected_running=False; db.commit(); process=psutil.Process(row.pid); process.terminate()
                try: row.exit_code=process.wait(timeout=self.stop_timeout)
                except psutil.TimeoutExpired: process.kill(); row.exit_code=process.wait(timeout=self.stop_timeout)
                row.ended_at=utcnow(); row.state="offline"; db.commit()
                if self.console_capture: self.console_capture.ended(bot.id,row.instance_id)
                executable,entry,cwd=self._configuration(bot); instance_id=f"INST-{uuid.uuid4()}"
                management_secret=secrets.token_urlsafe(48); bot.management_secret_hash=hashlib.sha256(management_secret.encode()).hexdigest()
                new=BotInstance(bot_id=bot.id,instance_id=instance_id,state="starting",expected_running=True,python_executable=executable,entry_file=entry,working_directory=cwd,supervisor_instance_id=self.instance_id); db.add(new); db.commit()
                env={k:v for k,v in os.environ.items() if k in {"PATH","HOME","USER","LOGNAME","LANG","LC_ALL","TMPDIR","TEMP","TMP","SYSTEMROOT","WINDIR"}}; env.update({"BOT_INSTANCE_ID":instance_id,"BOT_MANAGEMENT_BOT_ID":bot.id,"BOT_MANAGEMENT_SECRET":management_secret,"BOT_MANAGEMENT_HEARTBEAT_URL":f"http://{get_settings().supervisor_host}:{get_settings().supervisor_port}/internal/agent/heartbeat","BOT_HEARTBEAT_INTERVAL_SECONDS":str(get_settings().bot_heartbeat_interval_seconds)})
                process=subprocess.Popen([executable,entry],cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE if self.console_capture else subprocess.DEVNULL,stderr=subprocess.PIPE if self.console_capture else subprocess.DEVNULL,bufsize=0,start_new_session=os.name!="nt",creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0)
                created=datetime.fromtimestamp(psutil.Process(process.pid).create_time(),timezone.utc); new.pid=process.pid; new.process_created_at=created; new.started_at=created; new.state="running"; db.commit()
                if self.console_capture: self.console_capture.redactor.add_secrets((management_secret,)); self.console_capture.attach(process,bot.id,instance_id)
                return self._payload(new)
        finally: self._lock(bot_id).release()

    def reconcile(self,bot_id: str | None=None) -> list[dict]:
        with self.db_factory() as db:
            query=select(Bot).where(Bot.id==bot_id) if bot_id else select(Bot)
            result=[]
            for bot in db.scalars(query):
                row=self._current(db,bot.id)
                if row: self._reconcile_row(db,row)
                result.append({"bot_id":bot.id,**self._payload(row,bot.enabled)})
            db.commit(); return result

    def health(self) -> dict:
        rows=self.reconcile(); return {"status":"online","managed_processes":sum(x["process_running"] for x in rows),"registered_bots":len(rows),"supervisor_instance_id":self.instance_id}
