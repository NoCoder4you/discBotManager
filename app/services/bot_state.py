from dataclasses import dataclass
from datetime import datetime, timezone

from app.adapters.base import BotState


def aware(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class StateInputs:
    enabled: bool
    process_state: str
    process_running: bool
    expected_running: bool
    started_at: datetime | None = None
    operation: str | None = None
    connected: bool = False
    ready: bool = False
    last_heartbeat_at: datetime | None = None
    maintenance: bool = False
    supervisor_available: bool = True


class BotStateResolver:
    """The only policy boundary that combines process and Discord health."""
    def __init__(self, heartbeat_timeout: float, ready_timeout: float):
        self.heartbeat_timeout=heartbeat_timeout; self.ready_timeout=ready_timeout

    def resolve(self, value: StateInputs, now: datetime | None=None) -> BotState:
        now=now or datetime.now(timezone.utc)
        if not value.enabled: return BotState.DISABLED
        if value.operation=="restart" or value.process_state=="restarting": return BotState.RESTARTING
        if value.operation=="stop" or value.process_state=="stopping": return BotState.STOPPING
        if value.process_state=="crash_loop": return BotState.CRASH_LOOP
        if not value.supervisor_available or value.process_state=="unknown": return BotState.UNKNOWN
        if not value.process_running:
            return BotState.CRASHED if value.expected_running or value.process_state=="crashed" else BotState.OFFLINE
        heartbeat_age=(now-aware(value.last_heartbeat_at)).total_seconds() if value.last_heartbeat_at else None
        fresh=heartbeat_age is not None and heartbeat_age <= self.heartbeat_timeout
        if fresh and value.connected and value.ready:
            return BotState.MAINTENANCE if value.maintenance else BotState.ONLINE
        startup_age=(now-aware(value.started_at)).total_seconds() if value.started_at else self.ready_timeout+1
        if startup_age <= self.ready_timeout and (not fresh or (value.connected and not value.ready)):
            return BotState.STARTING
        return BotState.DISCONNECTED
