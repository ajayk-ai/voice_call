"""Command line for the one-time Google Sheet setup.

  python -m backend.po_followup setup-tabs          create missing tracking tabs in the Google Sheet
  python -m backend.po_followup build-phone-master  create the Phone_Master tab from Master's vendors
"""

import argparse

from backend.po_followup.config import Settings
from backend.po_followup.domain.escalation import map_row, txt
from backend.po_followup.domain.messages import TAB_HEADERS


def _sheet(settings: Settings):
    from backend.po_followup.integrations.sheets import PoSheet

    return PoSheet(settings.sheet_id)


def cmd_setup_tabs(settings: Settings) -> int:
    """'S0 Run Once – Create Tabs': adds missing tabs only, never touches existing ones."""
    sheet = _sheet(settings)
    for title, headers in TAB_HEADERS.items():
        if sheet.tab_exists(title):
            print(f"  {title}: exists, left unchanged")
        else:
            sheet.add_tab(title, headers)
            print(f"  {title}: created")
    return 0


def cmd_build_phone_master(settings: Settings) -> int:
    """'P0 Run Once – Build Phone Master': one row per vendor in Master. Never overwrites an existing tab."""
    sheet = _sheet(settings)
    if sheet.tab_exists("Phone_Master"):
        print("Phone_Master already exists; left unchanged so typed numbers are kept.")
        return 0
    vendors: dict[str, str] = {}
    for record in sheet.read_records("Master"):
        r = map_row(record)
        code = txt(r.get("vendorCode"))
        if code and (code not in vendors or (not vendors[code] and txt(r.get("vendorName")))):
            vendors[code] = txt(r.get("vendorName"))
    headers = ["Vendor Code", "Vendor Name", "Scenario 1 No", "Scenario 2 No", "Scenario 3 No"]
    rows = [{"Vendor Code": c, "Vendor Name": n} for c, n in sorted(vendors.items(), key=lambda kv: (len(kv[0]), kv[0]))]
    sheet.add_tab("Phone_Master", headers)
    sheet.append_rows("Phone_Master", rows)
    print(f"Phone_Master created with {len(rows)} vendors.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.po_followup", description="Google Sheet setup")
    parser.add_argument("command", choices=["setup-tabs", "build-phone-master"])
    args = parser.parse_args()
    settings = Settings.from_env()
    raise SystemExit(cmd_setup_tabs(settings) if args.command == "setup-tabs" else cmd_build_phone_master(settings))
