from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

class ScheduleType(str,Enum): INTERVAL="interval"; DAILY="daily"; WEEKLY="weekly"; MONTHLY="monthly"; ONE_TIME="one_time"
class ConcurrencyPolicy(str,Enum): FORBID_OVERLAP="forbid_overlap"; ALLOW_OVERLAP="allow_overlap"
class MisfirePolicy(str,Enum): SKIP="skip"; RUN_ONCE="run_once"
class TaskTrigger(str,Enum): MANUAL="manual"; SCHEDULED="scheduled"; RECOVERY="recovery"
class TaskRunStatus(str,Enum): QUEUED="queued"; RUNNING="running"; SUCCESS="success"; FAILED="failed"; SKIPPED="skipped"; TIMED_OUT="timed_out"; INTERRUPTED="interrupted"

@dataclass(frozen=True)
class TaskExecutionContext:
    bot_id:str; task_id:str; run_id:str; operation_id:str|None; trigger:TaskTrigger; triggered_by_user_id:int|None=None

@dataclass(frozen=True)
class TaskResult:
    summary:str="Task completed successfully."; details:dict[str,Any]=field(default_factory=dict)

TaskHandler=Callable[[TaskExecutionContext],Awaitable[TaskResult]]

@dataclass(frozen=True)
class RegisteredTask:
    id:str; name:str; description:str; handler:TaskHandler; category:str="General"
    enabled_by_default:bool=False; manual_run_allowed:bool=True; schedule_editable:bool=True; disable_allowed:bool=True
    timeout_seconds:int=120; concurrency_policy:ConcurrencyPolicy=ConcurrencyPolicy.FORBID_OVERLAP
    danger:str="low"; requires_process_running:bool=False; requires_discord_ready:bool=False; requires_process_offline:bool=False
    allowed_schedule_types:tuple[ScheduleType,...]=tuple(ScheduleType); misfire_policy:MisfirePolicy=MisfirePolicy.SKIP
    allow_while_bot_disabled:bool=False
