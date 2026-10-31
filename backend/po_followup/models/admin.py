"""Request bodies of the admin API (/admin/api/*)."""

from pydantic import BaseModel, ConfigDict, Field


class UserCreate(BaseModel):
    """An SCM user made on the admin page."""
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=60)
    password: str = Field(max_length=200)
    display_name: str = Field("", max_length=100)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(None, max_length=200)  # reset
    active: bool | None = None
    display_name: str | None = Field(None, max_length=100)


class WhatsAppCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mobile: str = Field(max_length=20)
    message: str = Field("", max_length=1000)  # blank = a sample Scenario 1 alert


class CallCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mobile: str = Field(max_length=20)


class MeetingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=200)
