"""Response of the business dashboard API (GET /dashboard/api/{env}); built by services.dashboard."""

from typing import Literal

from pydantic import BaseModel

ScenarioState = Literal["done", "due", "scheduled", "on_hold", "upcoming"]


class ScenarioStatus(BaseModel):
    """Where one line is in one scenario. `at`: when it was sent (done) or when it becomes due (scheduled)."""
    state: ScenarioState
    at: str | None = None
    simulated: bool = False  # sent by a dry run: no WhatsApp actually went out


class Scenario(BaseModel):
    name: str
    title: str
    who: str


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
    stage: int  # index into DashboardData.stages
    stage_name: str
    on_hold: bool  # pilot mode skips this vendor
    first_alerted: str
    scenarios: list[ScenarioStatus]  # Scenario 1, 2, 3
    meeting: bool  # the management meeting stage is reached


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


class DashboardData(BaseModel):
    env: Literal["prod", "test"]
    synced_at: str | None  # when Master was last copied from the Google Sheet
    now: str
    pilot_mode: bool
    scenarios: list[Scenario]
    stages: list[str]
    summary: Summary
    pos: list[PurchaseOrder]
