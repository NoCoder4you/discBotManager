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
    @property
    def secure_cookies(self) -> bool: return self.environment == "production"
    @model_validator(mode="after")
    def production_oauth(self):
        if self.environment == "production" and not all((self.discord_client_id, self.discord_client_secret, self.platform_owner_discord_id)):
            raise ValueError("Discord OAuth and PLATFORM_OWNER_DISCORD_ID are required in production")
        return self

@lru_cache
def get_settings() -> Settings: return Settings()  # type: ignore[call-arg]
