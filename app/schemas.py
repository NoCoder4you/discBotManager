import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator

BOT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,35}$")

class UserStatusUpdate(BaseModel): enabled: bool
class AssignmentMutation(BaseModel):
    bot_id: str = Field(min_length=2, max_length=36)
    role_key: Literal["viewer", "operator", "administrator"]
    enabled: bool = True
class PermissionOverrideMutation(BaseModel):
    permission_key: str = Field(min_length=3, max_length=100)
    state: Literal["inherit", "allow", "deny"]
class ProcessAction(BaseModel): action: Literal["start", "stop", "restart"]
class BotMutation(BaseModel):
    id: str = Field(min_length=2, max_length=36)
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    folder: str = Field(min_length=1, max_length=500)
    entry_file: str = Field(min_length=1, max_length=255)
    python_executable: str = Field(min_length=1, max_length=500)
    accent_colour: str = "#5865f2"
    enabled: bool = True
    adapter: str = Field(default="python", pattern=r"^[a-z0-9_-]+$")
    @field_validator("id")
    @classmethod
    def safe_id(cls, value):
        if not BOT_ID.fullmatch(value): raise ValueError("Bot ID must contain only lowercase letters, numbers, hyphens, or underscores")
        return value
    @field_validator("accent_colour")
    @classmethod
    def colour(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value): raise ValueError("Accent colour must be a six-digit hex colour")
        return value.lower()
