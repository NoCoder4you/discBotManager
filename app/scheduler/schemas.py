from __future__ import annotations
from datetime import datetime, timezone
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

MIN_INTERVAL_SECONDS=300; MAX_INTERVAL_SECONDS=31_536_000
class IntervalConfig(BaseModel):
    type:Literal["interval"]; every:int=Field(ge=1,le=365); unit:Literal["minutes","hours","days"]
    @model_validator(mode="after")
    def safe(self):
        seconds=self.every*{"minutes":60,"hours":3600,"days":86400}[self.unit]
        if not MIN_INTERVAL_SECONDS<=seconds<=MAX_INTERVAL_SECONDS: raise ValueError("Interval must be between 5 minutes and 365 days")
        return self
class ClockConfig(BaseModel):
    hour:int=Field(ge=0,le=23); minute:int=Field(ge=0,le=59)
class DailyConfig(ClockConfig): type:Literal["daily"]
class WeeklyConfig(ClockConfig): type:Literal["weekly"]; weekday:int=Field(ge=0,le=6)
class MonthlyConfig(ClockConfig):
    type:Literal["monthly"]; day:int=Field(ge=1,le=28,description="Limited to 28 so every month has an occurrence")
class OneTimeConfig(BaseModel):
    type:Literal["one_time"]; run_at:datetime
    @field_validator("run_at")
    @classmethod
    def future_aware(cls,value):
        if value.tzinfo is None: raise ValueError("run_at must include a timezone offset")
        if value.astimezone(timezone.utc)<=datetime.now(timezone.utc): raise ValueError("run_at must be in the future")
        return value
ScheduleConfig=Annotated[Union[IntervalConfig,DailyConfig,WeeklyConfig,MonthlyConfig,OneTimeConfig],Field(discriminator="type")]
SCHEDULE_ADAPTER=TypeAdapter(ScheduleConfig)
class ScheduleUpdate(BaseModel):
    enabled:bool; timezone:str=Field(min_length=1,max_length=64); config:ScheduleConfig
    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls,value):
        try: ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError) as exc: raise ValueError("Unknown IANA timezone") from exc
        return value
class ScheduleToggle(BaseModel): enabled:bool
class ManualTaskRun(BaseModel): confirmation:bool=False
class SupervisorTaskRun(BaseModel):
    trigger:Literal["manual","scheduled","recovery"]
    operation_id:str|None=Field(default=None,max_length=30,pattern=r"^[A-Z]+-[0-9]{6}$")
    user_id:int|None=Field(default=None,ge=1)
    actor:str=Field(default="SYSTEM",min_length=1,max_length=100)
