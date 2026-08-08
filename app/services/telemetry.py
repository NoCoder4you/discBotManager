from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import psutil
from sqlalchemy import select

from app.models import Bot, BotInstance


def aware(value): return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TelemetrySample:
    timestamp:str
    bot_id:str
    instance_id:str
    cpu_percent:float
    rss_bytes:int
    uptime_seconds:float
    def payload(self): return asdict(self)


class TelemetryStore:
    """Bounded, in-memory current and per-bot instance-aware history."""
    def __init__(self,capacity:int):
        self.capacity=capacity; self._latest={}; self._history=defaultdict(lambda:deque(maxlen=capacity)); self._lock=threading.Lock()
    def add(self,sample:TelemetrySample):
        with self._lock: self._latest[sample.bot_id]=sample; self._history[sample.bot_id].append(sample)
    def clear_current(self,bot_id,instance_id=None):
        with self._lock:
            current=self._latest.get(bot_id)
            if current and (instance_id is None or current.instance_id==instance_id): self._latest.pop(bot_id,None)
    def latest(self,bot_id,instance_id=None):
        with self._lock:
            sample=self._latest.get(bot_id)
            return sample if sample and (instance_id is None or sample.instance_id==instance_id) else None
    def history(self,bot_id,minutes:int,now=None):
        cutoff=(now or datetime.now(timezone.utc))-timedelta(minutes=minutes)
        with self._lock: return [sample for sample in self._history.get(bot_id,()) if datetime.fromisoformat(sample.timestamp.replace("Z","+00:00"))>=cutoff]


class TelemetryCollector:
    """One supervisor-owned psutil sampler for all validated managed instances."""
    def __init__(self,db_factory,identity_validator,interval=5,history_minutes=60,stale_after=15):
        self.db_factory=db_factory; self.identity_validator=identity_validator; self.interval=interval; self.history_minutes=history_minutes; self.stale_after=stale_after
        self.store=TelemetryStore(max(1,math.ceil(history_minutes*60/interval)+1)); self._processes={}; self._stop=threading.Event(); self._thread=None
    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread=threading.Thread(target=self._run,daemon=True,name="telemetry-collector"); self._thread.start()
    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=max(1,self.interval+1))
    def _targets(self):
        with self.db_factory() as db:
            bots=list(db.scalars(select(Bot)))
            result=[]
            for bot in bots:
                row=db.scalar(select(BotInstance).where(BotInstance.bot_id==bot.id).order_by(BotInstance.id.desc()).limit(1))
                if bot.enabled and row and row.state in {"running","starting"}: result.append(row)
            return result
    def collect_once(self,now=None):
        now=now or datetime.now(timezone.utc); active=set()
        try: targets=self._targets()
        except Exception: return
        for row in targets:
            key=(row.bot_id,row.instance_id); active.add(key)
            if not self.identity_validator(row): self.store.clear_current(row.bot_id,row.instance_id); self._processes.pop(key,None); continue
            try:
                process=self._processes.get(key)
                if process is None:
                    process=psutil.Process(row.pid); process.cpu_percent(None); self._processes[key]=process
                    continue
                created=datetime.fromtimestamp(process.create_time(),timezone.utc)
                if abs((created-aware(row.process_created_at)).total_seconds())>.02: raise psutil.NoSuchProcess(row.pid)
                sample=TelemetrySample(now.isoformat().replace("+00:00","Z"),row.bot_id,row.instance_id,max(0,float(process.cpu_percent(None))),int(process.memory_info().rss),max(0,(now-created).total_seconds()))
                self.store.add(sample)
            except (psutil.Error,OSError,ValueError): self.store.clear_current(row.bot_id,row.instance_id); self._processes.pop(key,None)
        for key in set(self._processes)-active: self._processes.pop(key,None); self.store.clear_current(*key)
    def _run(self):
        while not self._stop.is_set():
            try: self.collect_once()
            except Exception: pass
            self._stop.wait(self.interval)
    def current_payload(self,bot_id,instance_id=None):
        sample=self.store.latest(bot_id,instance_id)
        if not sample: return {"available":False,"stale":False,"sample":None}
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(sample.timestamp.replace("Z","+00:00"))).total_seconds(); stale=age>self.stale_after
        return {"available":not stale,"stale":stale,"sample":sample.payload() if not stale else None,"last_sample_at":sample.timestamp}
    def history_payload(self,bot_id,minutes): return [sample.payload() for sample in self.store.history(bot_id,minutes)]


def format_bytes(value:int|None)->str:
    if value is None: return "—"
    amount=float(value)
    for unit in ("B","KB","MB","GB"):
        if amount<1024 or unit=="GB": return f"{amount:.0f} {unit}" if unit=="B" else f"{amount:.1f} {unit}"
        amount/=1024
    return "—"


def format_uptime(seconds:float|None)->str:
    if seconds is None: return "—"
    total=max(0,int(seconds)); days,remainder=divmod(total,86400); hours,remainder=divmod(remainder,3600); minutes,secs=divmod(remainder,60)
    if days: return f"{days}d {hours}h"
    if hours: return f"{hours}h {minutes}m"
    if minutes: return f"{minutes}m {secs}s"
    return f"{secs}s"
