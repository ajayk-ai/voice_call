"""Settings for the PO follow-up workflow (was the '00 Config' node in n8n)."""

import os
from dataclasses import dataclass

import truststore

# Use the OS certificate store, so HTTPS works behind corporate SSL inspection (e.g. Sophos)
truststore.inject_into_ssl()

from dotenv import load_dotenv

load_dotenv()


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    sheet_id: str
    # Dry run: no WhatsApp sent, Line_Tracker / Call_Tasks / Meeting_Tasks not changed (Alert_Log still gets DRY_RUN rows)
    test_mode: bool
    update_tracker_in_test_mode: bool
    # Pilot: only pilot vendors are processed, and every message goes to pilot_mobile instead of the real number
    pilot_mode: bool
    pilot_vendor_codes: frozenset[str]
    pilot_mobile: str
    ultramsg_base_url: str
    ultramsg_token: str
    scm_hod_name: str
    scm_hod_mobile: str
    scm_head_email: str
    max_lines_per_message: int
    send_interval_seconds: float
    run_at: str  # daily run time, HH:MM in IST
    # Minutes to wait after level 0, 1, 2 before escalating to the next level
    intervals_minutes: tuple = (1440, 1440, 1440)
    # Scenario 3: the voice agent also rings the Scenario 3 number (pilot: pilot_mobile)
    agent_calls: bool = True
    # Day 3: a Google Meet invite is created; in pilot mode it only goes to pilot_email
    meet_invites: bool = True
    pilot_email: str = ""

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
            intervals_minutes=tuple(
                float(x) for x in os.getenv("FOLLOWUP_INTERVALS_MINUTES", "1440,1440,1440").split(",")
            ),
            agent_calls=env_bool("FOLLOWUP_AGENT_CALLS", True),
            meet_invites=env_bool("FOLLOWUP_MEET_INVITES", True),
            pilot_email=os.getenv("FOLLOWUP_PILOT_EMAIL", "").strip(),
        )

    @property
    def tracker_writes_enabled(self) -> bool:
        return not self.test_mode or self.update_tracker_in_test_mode
