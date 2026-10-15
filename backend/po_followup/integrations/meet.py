"""Day 3: a Google Calendar event with a Google Meet link, on GOOGLE_DELEGATED_USER's calendar.

Needs https://www.googleapis.com/auth/calendar.events added to the service account's client in
Workspace Admin > Security > API controls > Domain-wide delegation."""

import uuid
from datetime import datetime, timedelta

from google.auth.transport.requests import AuthorizedSession

from common.google_auth import delegated_credentials

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MEETING_MINUTES = 30


def calendar_session() -> AuthorizedSession:
    return AuthorizedSession(delegated_credentials(SCOPES))


class MeetScheduler:
    def schedule(self, title: str, description: str, start: datetime, attendees: list[str]) -> dict:
        """Create the event and email the invite to `attendees`. Returns {"event_link", "meet_link"}."""
        end = start + timedelta(minutes=MEETING_MINUTES)
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Kolkata"},
            "attendees": [{"email": e} for e in attendees],
            "conferenceData": {"createRequest": {"requestId": uuid.uuid4().hex,
                                                 "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        }
        r = calendar_session().post(EVENTS_URL, params={"conferenceDataVersion": 1, "sendUpdates": "all"},
                                    json=body, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Google Calendar error {r.status_code}: {r.text[:300]}")
        event = r.json()
        return {"event_link": event.get("htmlLink", ""), "meet_link": event.get("hangoutLink", "")}
