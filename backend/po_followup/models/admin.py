"""Request bodies and path types of the admin API (/admin/api/*)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Env = Literal["prod", "test"]
Mode = Literal["preview", "dry", "live"]
Table = Literal["Line_Tracker", "Alert_Log", "Call_Tasks", "Meeting_Tasks"]


class SettingsUpdate(BaseModel):
    """Any subset of the environment's settings; detailed checks are in services.followup.validate_settings."""
    model_config = ConfigDict(extra="forbid")

    intervals_minutes: list[float] | None = None
    auto_enabled: bool | None = None
    auto_mode: Literal["dry", "live"] | None = None
    schedule_type: Literal["daily", "interval"] | None = None
    daily_time: str | None = None
    every_minutes: float | None = None
    pilot_mode: bool | None = None
    pilot_mobile: str | None = None
    pilot_vendor_codes: str | None = None
    scm_hod_mobile: str | None = None
    max_lines_per_message: int | None = None
    refresh_from_sheet: bool | None = None
    confirm: str | None = None  # "LIVE" to turn on automatic live sending in production


class RunRequest(BaseModel):
    mode: Mode
    confirm: str | None = None  # "LIVE" for a live production run


class VendorTest(BaseModel):
    """Walk one test vendor through every scenario, `minutes` apart, with all messages going to `mobile`."""
    vendor_code: str = Field(min_length=1, max_length=40)
    mobile: str = Field(min_length=10, max_length=20)
    minutes: float = Field(1, ge=1, le=1440)
    mode: Literal["dry", "live"] = "live"
    auto: bool = True  # run the next scenarios automatically (needs the scheduler)


class VendorCall(BaseModel):
    """Ring `mobile` now with the voice agent reading this vendor's overdue lines (test data)."""
    vendor_code: str = Field(min_length=1, max_length=40)
    mobile: str = Field(min_length=10, max_length=20)
