"""Settings for the PO follow-up workflow (was the '00 Config' node in n8n)."""

import os
from dataclasses import dataclass

from backend.core.env import env_bool

__all__ = ["Settings", "env_bool"]


@dataclass(frozen=True)
class Settings:
    sheet_id: str
    # Dry run: no WhatsApp sent, Line_Tracker / Call_Tasks / Meeting_Tasks not changed (Alert_Log still gets DRY_RUN rows)
    test_mode: bool
    update_tracker_in_test_mode: bool
    # Pilot: only the selected vendors are processed, and every message goes to pilot_mobile instead of the real number
    pilot_mode: bool
    pilot_vendor_codes: frozenset[str]  # the selected vendors (chosen on the dashboard)
    pilot_mobile: str
    ultramsg_base_url: str
    ultramsg_token: str
    scm_hod_name: str
    scm_hod_mobile: str
    scm_head_email: str
    max_lines_per_message: int
    send_interval_seconds: float
    run_at: str  # daily run time, HH:MM in IST
    # Overdue lines are checked once per period (1440 = one IST day); each check in a row a line is still
    # overdue confirms the next scenario (domain/overdue.py)
    check_period_minutes: float = 1440
    # Scenario 3: the voice agent also rings the Scenario 3 number (pilot: pilot_call_mobile, else pilot_mobile)
    agent_calls: bool = True
    # Pilot: the number the Scenario 3 agent call rings; blank = pilot_mobile (WhatsApp messages still go to pilot_mobile)
    pilot_call_mobile: str = ""
    # Day 3: a Google Meet invite is created; in pilot mode it only goes to pilot_email
    meet_invites: bool = True
    pilot_email: str = ""
    # "selected": only pilot_vendor_codes are followed up (to their real numbers unless pilot_mode); "all": every vendor
    vendor_scope: str = "selected"

    @property
    def vendor_filter(self) -> frozenset[str] | None:
        """The only vendors a run follows up, or None for every vendor."""
        return self.pilot_vendor_codes if self.pilot_mode or self.vendor_scope != "all" else None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            sheet_id=os.getenv("PO_SHEET_ID", ""),
            test_mode=env_bool("FOLLOWUP_TEST_MODE", True),
            update_tracker_in_test_mode=env_bool("FOLLOWUP_UPDATE_TRACKER_IN_TEST_MODE", False),
            pilot_mode=env_bool("FOLLOWUP_PILOT_MODE", True),
            pilot_vendor_codes=frozenset(
                c.strip() for c in os.getenv("FOLLOWUP_PILOT_VENDOR_CODES", "400186").split(",") if c.strip()
            ),
            pilot_mobile=os.getenv("FOLLOWUP_PILOT_MOBILE", ""),
            ultramsg_base_url=os.getenv("ULTRAMSG_BASE_URL", "").rstrip("/"),
            ultramsg_token=os.getenv("ULTRAMSG_TOKEN", ""),
            scm_hod_name=os.getenv("SCM_HOD_NAME", "SCM Head"),
            scm_hod_mobile=os.getenv("SCM_HOD_MOBILE", ""),
            scm_head_email=os.getenv("SCM_HEAD_EMAIL", ""),
            max_lines_per_message=int(os.getenv("FOLLOWUP_MAX_LINES_PER_MESSAGE", "15")),
            send_interval_seconds=float(os.getenv("FOLLOWUP_SEND_INTERVAL_SECONDS", "1.5")),
            run_at=os.getenv("FOLLOWUP_RUN_AT", "08:30"),
            check_period_minutes=float(os.getenv("FOLLOWUP_CHECK_PERIOD_MINUTES", "1440")),
            agent_calls=env_bool("FOLLOWUP_AGENT_CALLS", True),
            pilot_call_mobile=os.getenv("FOLLOWUP_PILOT_CALL_MOBILE", "").strip(),
            meet_invites=env_bool("FOLLOWUP_MEET_INVITES", True),
            pilot_email=os.getenv("FOLLOWUP_PILOT_EMAIL", "").strip(),
            vendor_scope="all" if os.getenv("FOLLOWUP_VENDOR_SCOPE", "selected").strip().lower() == "all" else "selected",
        )

    @property
    def tracker_writes_enabled(self) -> bool:
        return not self.test_mode or self.update_tracker_in_test_mode
