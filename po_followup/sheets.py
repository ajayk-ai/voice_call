"""Google Sheets access with a service account + domain-wide delegation.

Credentials come from GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents, for servers like Render)
or GOOGLE_SERVICE_ACCOUNT_FILE (a path, for local use), impersonating GOOGLE_DELEGATED_USER."""

import json
import logging
import os

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1

log = logging.getLogger("po_followup.sheets")

# Must match a scope authorized for this client in Workspace Admin > Domain-wide delegation
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsNotConfigured(RuntimeError):
    pass


def load_credentials() -> Credentials:
    user = os.getenv("GOOGLE_DELEGATED_USER")
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not user or not (raw_json or path):
        raise SheetsNotConfigured(
            "Set GOOGLE_DELEGATED_USER and GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE"
        )
    if raw_json:
        creds = Credentials.from_service_account_info(json.loads(raw_json), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return creds.with_subject(user)


def records_from_values(values: list[list]) -> list[dict]:
    """First row = headers. Blank headers are dropped; for a repeated header the first column wins."""
    if not values:
        return []
    headers = [str(h).strip() for h in values[0]]
    out = []
    for row in values[1:]:
        if not any(str(c).strip() for c in row):
            continue
        rec: dict = {}
        for i, h in enumerate(headers):
            if h and h not in rec:
                rec[h] = row[i] if i < len(row) else ""
        out.append(rec)
    return out


SHEETS_TIMEOUT_SECONDS = 60


class PoSheet:
    """The 'CE - Pending PO' spreadsheet and its tracking tabs."""

    def __init__(self, sheet_id: str):
        if not sheet_id:
            raise SheetsNotConfigured("Set PO_SHEET_ID")
        client = gspread.authorize(load_credentials())
        client.set_timeout(SHEETS_TIMEOUT_SECONDS)  # a hung request must not block the run
        self.spreadsheet = client.open_by_key(sheet_id)

    def tab(self, title: str) -> gspread.Worksheet:
        return self.spreadsheet.worksheet(title)

    def tab_exists(self, title: str) -> bool:
        return any(ws.title == title for ws in self.spreadsheet.worksheets())

    def read_records(self, title: str) -> list[dict]:
        return records_from_values(self.tab(title).get_all_values())

    def append_rows(self, title: str, rows: list[dict]) -> None:
        """Append rows, matching dict keys to the tab's header row (unknown keys are ignored)."""
        if not rows:
            return
        ws = self.tab(title)
        header = ws.row_values(1)
        if not header:
            header = list(rows[0].keys())
            ws.update([header], "A1", value_input_option=ValueInputOption.raw)
        unknown = {k for r in rows for k in r} - set(header)
        if unknown:
            log.warning("%s has no column(s) %s; those values were not written", title, sorted(unknown))
        ws.append_rows(
            [[_cell(r.get(h, "")) for h in header] for r in rows],
            value_input_option=ValueInputOption.raw,
            insert_data_option="INSERT_ROWS",
            table_range="A1",
        )

    def upsert_rows(self, title: str, rows: list[dict], key: str) -> None:
        """Update the given columns of rows whose `key` column matches; append the rest."""
        if not rows:
            return
        ws = self.tab(title)
        values = ws.get_all_values()
        header = values[0] if values else []
        if key not in header:
            raise ValueError(f"{title} has no '{key}' column")
        key_col = header.index(key)
        existing = {row[key_col]: n for n, row in enumerate(values[1:], start=2) if key_col < len(row) and row[key_col]}

        updates, new_rows = [], []
        for r in rows:
            row_number = existing.get(str(r[key]))
            if row_number is None:
                new_rows.append(r)
                continue
            for col_name, value in r.items():
                if col_name in header:
                    updates.append({"range": rowcol_to_a1(row_number, header.index(col_name) + 1), "values": [[_cell(value)]]})
        if updates:
            ws.batch_update(updates, value_input_option=ValueInputOption.raw)
        if new_rows:
            self.append_rows(title, new_rows)

    def add_tab(self, title: str, headers: list[str]) -> None:
        ws = self.spreadsheet.add_worksheet(title=title, rows=1000, cols=max(len(headers), 10))
        ws.update([headers], "A1", value_input_option=ValueInputOption.raw)


def _cell(value):
    return "" if value is None else value
