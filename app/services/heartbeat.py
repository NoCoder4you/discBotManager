import hmac
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import ActivityEvent, Bot, BotInstance, utcnow
from app.schemas import AgentHeartbeat


class HeartbeatRejected(RuntimeError):
    def __init__(self, message: str, status_code: int=409): super().__init__(message); self.status_code=status_code


def aware(value): return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


class HeartbeatService:
    """Authenticates and monotonically updates current-instance Discord state."""
    def __init__(self, db_factory, clock_skew: float=60, minimum_interval: float=.5, heartbeat_timeout: float=30):
        self.db_factory=db_factory; self.clock_skew=clock_skew; self.minimum_interval=minimum_interval; self.heartbeat_timeout=heartbeat_timeout

    @staticmethod
    def _current(db, bot_id):
        return db.scalar(select(BotInstance).where(BotInstance.bot_id==bot_id).order_by(BotInstance.id.desc()).limit(1))

    @staticmethod
    def _event(db,event_type,row,**payload):
        db.add(ActivityEvent(event_type=event_type,bot_id=row.bot_id,payload={"source":"SYSTEM","instance_id":row.instance_id,**payload}))

    def accept(self, heartbeat: AgentHeartbeat, credential: str | None) -> dict:
        now=datetime.now(timezone.utc); sent=aware(heartbeat.timestamp)
        with self.db_factory() as db:
            bot=db.get(Bot,heartbeat.bot_id)
            supplied=hashlib.sha256(credential.encode()).hexdigest() if credential else ""
            if not bot or not bot.management_secret_hash or not credential or not hmac.compare_digest(bot.management_secret_hash,supplied):
                raise HeartbeatRejected("Invalid agent credential",401)
            row=self._current(db,bot.id)
            if not row or row.instance_id!=heartbeat.instance_id or row.ended_at is not None:
                raise HeartbeatRejected("Heartbeat does not belong to the current instance")
            if not bot.enabled: raise HeartbeatRejected("Bot registration is disabled")
            if row.state not in {"running","starting","restarting"}:
                raise HeartbeatRejected("Current process is not running")
            if abs((now-sent).total_seconds())>self.clock_skew: raise HeartbeatRejected("Heartbeat timestamp is outside allowed clock skew")
            previous_sent=aware(row.last_agent_timestamp)
            if previous_sent and sent<=previous_sent: raise HeartbeatRejected("Heartbeat is out of order")
            received=aware(row.last_heartbeat_at)
            if received and (now-received).total_seconds()<self.minimum_interval: raise HeartbeatRejected("Heartbeat rate limit exceeded",429)
            was_connected=row.discord_connected; was_ready=row.discord_ready; was_stale=bool(received and (now-received).total_seconds()>self.heartbeat_timeout)
            row.last_agent_timestamp=sent; row.last_heartbeat_at=now
            row.discord_connected=heartbeat.connected; row.discord_ready=heartbeat.ready
            row.discord_latency_ms=heartbeat.latency_ms; row.guild_count=heartbeat.guild_count
            if heartbeat.connected and not was_connected:
                row.connected_at=now; self._event(db,"BOT_DISCORD_CONNECTED",row)
            if not heartbeat.connected and was_connected:
                row.last_disconnect_at=now; self._event(db,"BOT_DISCONNECTED",row)
            if heartbeat.ready and not was_ready:
                recovered=row.ready_at is not None; row.ready_at=row.ready_at or now; row.last_ready_at=now
                self._event(db,"BOT_READY_RECOVERED" if recovered else "BOT_READY",row)
            if was_stale and received: self._event(db,"BOT_HEARTBEAT_RECOVERED",row)
            db.commit()
            return {"accepted":True,"received_at":now.isoformat()}

    def invalidate(self, row: BotInstance):
        row.discord_connected=False; row.discord_ready=False; row.discord_latency_ms=None; row.guild_count=None
