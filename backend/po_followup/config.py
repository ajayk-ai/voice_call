"""Settings for the PO follow-up workflow (was the '00 Config' node in n8n)."""

import os
from dataclasses import dataclass

from backend.core.env import env_bool, env_int

__all__ = ["Settings", "env_bool"]


@dataclass(frozen=True)
class Settings:
    sheet_id: str
    # Trial: nothing is sent and the tracker and tasks do not change (Alert_Log still gets DRY_RUN rows)
    dry_run: bool
    vendor_codes: frozenset[str]  # the vendors chosen for follow-up
    ultramsg_base_url: str
    ultramsg_token: str
    scm_hod_name: str
    scm_hod_mobile: str
    scm_head_email: str
    max_lines_per_message: int
    send_interval_seconds: float
    # Overdue lines are checked once per period (1440 = one IST day); each check in a row a line is still
    # overdue confirms the next scenario (domain/overdue.py)
    check_period_minutes: float = 1440
    # Scenario 3: the voice agent also rings the Scenario 3 number
    agent_calls: bool = True
    # Day 3: a Google Meet invite is created
    meet_invites: bool = True
    # "selected": only vendor_codes are followed up; "all": every vendor
    vendor_scope: str = "selected"
    # Days after a vendor reaches Scenario 4 at which it starts again from Scenario 1
    cycle_days: int = 7
    # "Restart from Scenario 1" was pressed at this time (IST, as in Alert_Log): nothing before it counts
    fresh_start: str = ""

    @property
    def vendor_filter(self) -> frozenset[str] | None:
        """The only vendors a run follows up, or None for every vendor."""
        return None if self.vendor_scope == "all" else self.vendor_codes

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            sheet_id=os.getenv("PO_SHEET_ID", ""),
            dry_run=True,
            vendor_codes=frozenset(),
            ultramsg_base_url=os.getenv("ULTRAMSG_BASE_URL", "").rstrip("/"),
            ultramsg_token=os.getenv("ULTRAMSG_TOKEN", ""),
            scm_hod_name=os.getenv("SCM_HOD_NAME", "SCM Head"),
            scm_hod_mobile=os.getenv("SCM_HOD_MOBILE", ""),
            scm_head_email=os.getenv("SCM_HEAD_EMAIL", ""),
            max_lines_per_message=int(os.getenv("FOLLOWUP_MAX_LINES_PER_MESSAGE", "15")),
            send_interval_seconds=float(os.getenv("FOLLOWUP_SEND_INTERVAL_SECONDS", "1.5")),
            agent_calls=env_bool("FOLLOWUP_AGENT_CALLS", True),
            meet_invites=env_bool("FOLLOWUP_MEET_INVITES", True),
            cycle_days=max(1, env_int("FOLLOWUP_CYCLE_DAYS", 7)),
        )
