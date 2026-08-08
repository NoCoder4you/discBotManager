from __future__ import annotations

import asyncio
import logging
import re
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO

BOT_ID=re.compile(r"^[a-z0-9][a-z0-9_-]{1,35}$")
ANSI=re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
CONTROL=re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TOKEN_PATTERNS=(
    re.compile(r"(?<![\w-])(?:[A-Za-z\d_-]{24,28}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{25,110})(?![\w-])"),
    re.compile(r"(?i)(?P<label>(?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+"),
)


class SecretRedactor:
    """Single sanitisation boundary used before all console destinations."""
    def __init__(self,secrets=()): self._lock=threading.Lock(); self._secrets=set(); self.add_secrets(secrets)
    def add_secrets(self,values):
        with self._lock: self._secrets.update(str(v) for v in values if v and len(str(v))>=8)
    def redact(self,text:str)->str:
        safe=CONTROL.sub("",ANSI.sub("",text))
        with self._lock:
            for secret in sorted(self._secrets,key=len,reverse=True): safe=safe.replace(secret,"[REDACTED]")
        for pattern in TOKEN_PATTERNS:
            safe=pattern.sub(lambda m:(m.groupdict().get("label") or "")+"[REDACTED]",safe)
        return safe


@dataclass(frozen=True)
class ConsoleRecord:
    sequence:int; timestamp:str; bot_id:str; instance_id:str; stream:str; message:str
    def payload(self): return {"type":"console_record",**asdict(self)}


class ConsoleBroker:
    """Thread-safe, bounded current-history broker owned by the supervisor."""
    def __init__(self,buffer_lines:int=5000):
        self.buffer_lines=buffer_lines; self.stream_id=str(uuid.uuid4()); self._records=defaultdict(lambda:deque(maxlen=buffer_lines)); self._sequence=0; self._lock=threading.Lock()
    def publish(self,bot_id,instance_id,stream,message):
        with self._lock:
            self._sequence+=1; record=ConsoleRecord(self._sequence,datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),bot_id,instance_id,stream,message)
            self._records[bot_id].append(record); return record
    def records(self,bot_id,after:int=0,limit:int=1000):
        with self._lock: return [x.payload() for x in self._records.get(bot_id,()) if x.sequence>after][:limit]


class ConsoleCapture:
    """Continuously drains one process generation's stdout and stderr."""
    def __init__(self,broker,redactor,logs_root,max_line_length=16384,max_bytes=10485760,backup_count=5):
        self.broker=broker; self.redactor=redactor; self.logs_root=Path(logs_root).resolve(); self.max_line_length=max_line_length; self.max_bytes=max_bytes; self.backup_count=backup_count
        self._handlers={}; self._threads=set(); self.persistence_available={}
    def _handler(self,bot_id):
        if not BOT_ID.fullmatch(bot_id): raise ValueError("Invalid bot ID")
        target=(self.logs_root/bot_id).resolve()
        if not target.is_relative_to(self.logs_root): raise ValueError("Unsafe console log path")
        target.mkdir(parents=True,exist_ok=True); handler=RotatingFileHandler(target/"console.log",maxBytes=self.max_bytes,backupCount=self.backup_count,encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s")); self._handlers[bot_id]=handler; return handler
    def emit(self,bot_id,instance_id,stream,text):
        suffix=" [output truncated]"; text=text.rstrip("\r\n")
        safe=self.redactor.redact(text)
        if len(safe)>self.max_line_length: safe=safe[:max(0,self.max_line_length-len(suffix))]+suffix
        record=self.broker.publish(bot_id,instance_id,stream,safe)
        try:
            handler=self._handlers.get(bot_id) or self._handler(bot_id); handler.handle(logging.LogRecord("console",logging.INFO,"",0,f"{record.timestamp} {instance_id} {stream} {safe}",(),None)); self.persistence_available[bot_id]=True
        except Exception: self.persistence_available[bot_id]=False
        return record
    def _drain(self,pipe:BinaryIO,bot_id,instance_id,stream):
        try:
            while True:
                raw=pipe.readline(self.max_line_length*4+1)
                if not raw: break
                self.emit(bot_id,instance_id,stream,raw.decode("utf-8",errors="replace"))
        finally:
            try: pipe.close()
            except OSError: pass
            self._threads.discard(threading.current_thread())
    def attach(self,process,bot_id,instance_id):
        self.emit(bot_id,instance_id,"system",f"--- Instance {instance_id} started ---")
        for stream,pipe in (("stdout",process.stdout),("stderr",process.stderr)):
            if pipe is None: continue
            thread=threading.Thread(target=self._drain,args=(pipe,bot_id,instance_id,stream),daemon=True,name=f"console-{bot_id}-{stream}"); self._threads.add(thread); thread.start()
    def ended(self,bot_id,instance_id): self.emit(bot_id,instance_id,"system",f"--- Instance {instance_id} ended ---")


class SlowSubscriber(RuntimeError): pass


class ConsoleSubscriptionManager:
    """Bounded FastAPI fan-out queues plus revocation flags."""
    def __init__(self,queue_size=1000,max_per_user=3,max_per_bot=25):
        self.queue_size=queue_size; self.max_per_user=max_per_user; self.max_per_bot=max_per_bot; self._items={}; self._last=defaultdict(int); self._streams={}; self._lock=threading.Lock(); self._next=0
    def subscribe(self,user_id,bot_id):
        with self._lock:
            if sum(x[0]==user_id for x in self._items.values())>=self.max_per_user or sum(x[1]==bot_id for x in self._items.values())>=self.max_per_bot: raise SlowSubscriber("Console connection limit reached")
            self._next+=1; token=self._next; queue=asyncio.Queue(self.queue_size); self._items[token]=(user_id,bot_id,queue,False); return token,queue
    def publish(self,bot_id,records,stream_id=None):
        with self._lock:
            if stream_id is not None and self._streams.get(bot_id)!=stream_id: self._streams[bot_id]=stream_id; self._last[bot_id]=0
            records=[record for record in records if record.get("sequence",0)>self._last[bot_id]]
            if records: self._last[bot_id]=max(record["sequence"] for record in records)
            for token,(user,current,queue,revoked) in list(self._items.items()):
                if current!=bot_id or revoked: continue
                for record in records:
                    try: queue.put_nowait(record)
                    except asyncio.QueueFull: self._items[token]=(user,current,queue,True); break
    def revoke(self,user_id,bot_id=None):
        with self._lock:
            for token,(user,current,queue,_) in list(self._items.items()):
                if user==user_id and (bot_id is None or current==bot_id): self._items[token]=(user,current,queue,True)
    def revoked(self,token):
        with self._lock: return token not in self._items or self._items[token][3]
    def unsubscribe(self,token):
        with self._lock: self._items.pop(token,None)
