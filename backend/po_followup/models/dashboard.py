"""Response of the business dashboard API (GET /dashboard/api/{env}); built by services.dashboard."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScenarioState = Literal["done", "due", "on_hold", "queued", "upcoming"]


class ScenarioStatus(BaseModel):
    """Where one line is in one scenario (see services.dashboard.scenario_states).
    `at`: when it was sent (done), or the check that confirms it if the line is still overdue then (upcoming)."""
    state: ScenarioState
    at: str | None = None
    simulated: bool = False  # sent by a dry run: no WhatsApp actually went out


class Scenario(BaseModel):
    name: str
    title: str
    who: str


class CheckMark(BaseModel):
    """One daily overdue check of the line: the period it covers and whether the line was overdue."""
    at: str
    overdue: bool


class Line(BaseModel):
    key: str
    item: str
    sched: str
    material: str
    desc: str
    pending: str
    uom: str
    od: float  # days overdue
    due: str
    stage: int  # scenario confirmed by the overdue streak: index into DashboardData.stages
    stage_name: str
    streak: int  # checks in a row (including now) the line has been overdue
    on_hold: bool  # this vendor is not selected for follow-up
    first_alerted: str
    scenarios: list[ScenarioStatus]  # Scenario 1, 2, 3
    meeting: bool  # the management meeting task has been created
    history: list[CheckMark]  # last checks, oldest first


class PurchaseOrder(BaseModel):
    po: str
    vendor_code: str
    vendor_name: str
    buyer: str
    purchasing_group: str
    po_date: str
    max_od: float
    stage: int  # furthest stage among its lines
    lines: list[Line]


class Summary(BaseModel):
    lines: int
    pos: int
    vendors: int
    max_od: float
    over_30: int
    by_stage: list[int]
    cleared_recent: int
    cleared_days: int
    earlier_checks: int  # recorded checks before the current one that the streaks are based on


class DashboardData(BaseModel):
    env: Literal["prod", "test"]
    synced_at: str | None  # when Master was last copied from the Google Sheet
    now: str
    pilot_mode: bool  # messages go to the pilot number instead of the vendors
    selected_only: bool  # only the selected vendors are followed up; the rest show On hold
    selected_vendors: int
    check_period_minutes: float  # 1440 = one check per day
    last_check: str | None  # the most recent recorded check before the current period
    scenarios: list[Scenario]
    stages: list[str]
    summary: Summary
    pos: list[PurchaseOrder]


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


class VendorNumbersList(BaseModel):
    env: Literal["prod", "test"]
    saves_to: str
    ready: bool  # Phone_Master exists and has a Vendor Code column
    scope: Literal["selected", "all"]  # runs follow up only the selected vendors, or every vendor
    pilot_mode: bool  # messages go to the pilot number instead of the vendors (admin setting)
    unknown_selected: list[str]
    vendors: list[VendorNumbers]


class VendorNumbersUpdate(BaseModel):
    """New Scenario 1/2/3 numbers of one vendor; blank = use Master's. Several numbers: separate with commas."""
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, max_length=200)  # only needed when adding a vendor
    numbers: list[str] = Field(min_length=3, max_length=3)


class VendorSelection(BaseModel):
    """Which vendors the follow-up runs for."""
    model_config = ConfigDict(extra="forbid")

    scope: Literal["selected", "all"]
    codes: list[str] = Field(max_length=5000)
    confirm: str | None = None  # "ALL" to follow up every vendor in production


class BulkRow(BaseModel):
    """One row of an upload. None = leave as it is; "" for a number = blank (use Master's)."""
    model_config = ConfigDict(extra="forbid")

    line: int | None = None  # row number in the file, for messages
    code: str = Field(max_length=40)
    name: str | None = Field(None, max_length=200)
    numbers: list[str | None] = Field([None, None, None], min_length=3, max_length=3)
    follow: bool | None = None


class BulkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[BulkRow] = Field(min_length=1, max_length=5000)
    apply: bool = False  # False: only check the rows and report what would change


class BulkResult(BaseModel):
    applied: bool
    numbers_updated: int
    numbers_added: int
    selected_added: list[str]
    selected_removed: list[str]
    unchanged: int
