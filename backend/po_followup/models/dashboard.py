"""Request and response bodies of the SCM user API (/dashboard/api/*)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------- vendors: follow-up selection and Scenario numbers (Phone_Master), see services.vendor_numbers ----------

class VendorNumbers(BaseModel):
    code: str
    name: str
    in_phone_master: bool  # False: only in Master, so Master's numbers are used
    in_master: bool
    overdue_lines: int
    selected: bool  # chosen for follow-up
    numbers: list[str]  # Scenario 1, 2, 3 as in Phone_Master ("" = use Master's)
    fallback: list[str]  # Master's numbers used when the Phone_Master cell is blank
    emails: str = ""  # Scenario 4: the vendor's own e-mail ids for the management meeting
    master_emails: str = ""  # buyer / buyer lead / SCM head ids from Master, which the meeting gets as well


class VendorNumbersList(BaseModel):
    ready: bool  # Phone_Master exists and has a Vendor Code column
    scope: Literal["selected", "all"]  # runs follow up only the selected vendors, or every vendor
    unknown_selected: list[str]
    vendors: list[VendorNumbers]


class VendorNumbersUpdate(BaseModel):
    """New Scenario 1/2/3 numbers of one vendor; blank = use Master's. Several numbers: separate with commas."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=200)  # only needed when adding a vendor
    numbers: list[str] = Field(min_length=3, max_length=3)
    emails: str | None = Field(None, max_length=1000)  # Scenario 4; None = leave as it is, "" = remove


class VendorSelection(BaseModel):
    """Which vendors the follow-up runs for."""
    model_config = ConfigDict(extra="forbid")

    scope: Literal["selected", "all"]
    codes: list[str] = Field(max_length=5000)
    confirm: str | None = None  # "ALL" to follow up every vendor


class BulkRow(BaseModel):
    """One row of an upload. None = leave as it is; "" for a number = blank (use Master's)."""
    model_config = ConfigDict(extra="forbid")

    line: int | None = None  # row number in the file, for messages
    code: str = Field(max_length=40)
    name: str | None = Field(None, max_length=200)
    numbers: list[str | None] = Field([None, None, None], min_length=3, max_length=3)
    follow: bool | None = None
    emails: str | None = Field(None, max_length=1000)


class BulkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[BulkRow] = Field(min_length=1, max_length=5000)
    apply: bool = False  # False: only check the rows and report what would change


class BulkResult(BaseModel):
    applied: bool
    numbers_updated: int
    numbers_added: int
    emails_updated: int = 0
    selected_added: list[str]
    selected_removed: list[str]
    unchanged: int


# ---------- run control (services.control) ----------

class ControlStart(BaseModel):
    """Start the automatic follow-up: repeat every `every_minutes` (default one day), or daily at `daily_time`."""
    model_config = ConfigDict(extra="forbid")

    schedule_type: Literal["interval", "daily"] = "interval"
    every_minutes: float | None = Field(None, ge=1)  # None = FOLLOWUP_DEFAULT_INTERVAL_MINUTES; longest: FOLLOWUP_MAX_INTERVAL_DAYS
    daily_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    mode: Literal["dry", "live"] = "dry"  # dry: nothing is sent, the dashboard shows what would be
    confirm: str | None = None  # "LIVE" to start live sending
    scope: Literal["selected", "all"] | None = None  # vendors to follow up, chosen together with Start; None = as saved
    codes: list[str] = Field(default_factory=list, max_length=5000)  # with scope "selected"
    confirm_scope: str | None = None  # "ALL" to follow up every vendor


class ControlRestart(ControlStart):
    """Every vendor back to Scenario 1, then start with these settings."""
    confirm_restart: str | None = None  # "RESTART"


class RunNow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry", "live"] = "dry"
    confirm: str | None = None  # "LIVE" for a live run
