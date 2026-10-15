"""Google service account credentials with domain-wide delegation (Sheets, Calendar, the sheet sync).

The key comes from GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents, for servers like Render) or
GOOGLE_SERVICE_ACCOUNT_FILE (a path, for local use); GOOGLE_DELEGATED_USER is the Workspace user impersonated.
Each scope must be authorized for the client in Workspace Admin > Security > API controls > Domain-wide delegation."""

import json
import os

from google.oauth2.service_account import Credentials

from backend.core import env  # noqa: F401 - loads .env


class GoogleNotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("GOOGLE_DELEGATED_USER")
                and (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")))


def delegated_credentials(scopes: list[str]) -> Credentials:
    if not configured():
        raise GoogleNotConfigured(
            "Set GOOGLE_DELEGATED_USER and GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        creds = Credentials.from_service_account_info(json.loads(raw_json), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=scopes)
    return creds.with_subject(os.environ["GOOGLE_DELEGATED_USER"])
