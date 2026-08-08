from functools import lru_cache
from typing import Literal
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_secret: str = Field(min_length=32)
    database_url: str = "sqlite:///./platform.db"
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    platform_owner_discord_id: str = ""
    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    bot_root: str = "."
    supervisor_secret: str = Field(default="", min_length=0)
    supervisor_url: str = "http://127.0.0.1:8765"
    supervisor_host: str = "127.0.0.1"
    supervisor_port: int = Field(default=8765, ge=1, le=65535)
    supervisor_timeout: float = Field(default=3.0, gt=0, le=30)
    supervisor_stop_timeout: float = Field(default=10.0, gt=0, le=120)
    @property
    def secure_cookies(self) -> bool: return self.environment == "production"
    @model_validator(mode="after")
    def production_oauth(self):
        if self.environment == "production" and not all((self.discord_client_id, self.discord_client_secret, self.platform_owner_discord_id)):
            raise ValueError("Discord OAuth and PLATFORM_OWNER_DISCORD_ID are required in production")
        if self.environment == "production" and len(self.supervisor_secret) < 32:
            raise ValueError("SUPERVISOR_SECRET must contain at least 32 characters in production")
        return self

@lru_cache
def get_settings() -> Settings: return Settings()  # type: ignore[call-arg]
