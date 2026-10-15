"""Command line for the PO follow-up workflow.

  python -m backend.po_followup admin               whole web app on http://127.0.0.1:8100 (admin at /admin) + scheduler
  python -m backend.po_followup run --env test --mode preview   one run now (env: prod/test, mode: preview/dry/live)
  python -m backend.po_followup scheduler           automatic scheduler only (no web page)
  python -m backend.po_followup copy-prod-to-test   refresh test data from production
  python -m backend.po_followup setup-tabs          create missing tracking tabs in the Google Sheet
  python -m backend.po_followup build-phone-master  create the Phone_Master tab from Master's vendors
"""

import argparse
import logging
import signal
import sys
import threading

from backend.core.tracing import flush

from backend.po_followup.config import Settings
from backend.po_followup.domain.escalation import map_row, txt
from backend.po_followup.domain.messages import TAB_HEADERS

log = logging.getLogger("po_followup")


def _sheet(settings: Settings):
    from backend.po_followup.integrations.sheets import PoSheet

    return PoSheet(settings.sheet_id)


def cmd_admin(port: int) -> int:
    """The full web app (calls, voice agent, admin) on this PC only, with the automatic scheduler."""
    import os

    import uvicorn

    os.environ["FOLLOWUP_SCHEDULER"] = "true"
    log.info("Admin page: http://127.0.0.1:%d/admin", port)
    uvicorn.run("backend.voice_agent.app:app", host="127.0.0.1", port=port, log_level="info")
    return 0


def cmd_run(env: str, mode: str) -> int:
    from backend.po_followup.services import followup as service
    from backend.po_followup.db import store

    store.init_environment(env)
    if env == "prod" and mode == "live" and input("LIVE production run. Type LIVE to continue: ") != "LIVE":
        print("Cancelled.")
        return 1
    run_id = service.start_run(env, mode, "manual", wait=True)
    r = service.get_run(env, run_id)
    print(f"\nRun #{run_id} ({env}, {mode}): {r['status']}")
    if r["error"]:
        print("  error:", r["error"])
    for a in (r.get("report") or {}).get("alerts", []):
        print(f"  L{a['level']} {a['alertType']:<22} {a['vendor'][:40]:<40} {a['role']:<32} {a['mobile'] or '-':<14} {a.get('status', '')}")
    print("  summary:", r["summary"])
    return 0 if r["status"] == "ok" else 1


def cmd_scheduler() -> int:
    from backend.po_followup.services import followup as service
    from backend.po_followup.db import store

    for env in store.ENVIRONMENTS:
        store.init_environment(env)
    service.mark_interrupted_runs()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    scheduler = service.Scheduler()
    scheduler.start()
    log.info("Scheduler running (settings from the admin page). Ctrl+C to stop.")
    stop.wait()
    scheduler.stop()
    return 0


def cmd_copy_prod_to_test() -> int:
    from backend.po_followup.db import store

    store.init_environment("test")
    print("Copied:", store.copy_prod_to_test())
    return 0


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
    parser = argparse.ArgumentParser(prog="python -m backend.po_followup", description="SCM PO follow-up workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    admin_p = sub.add_parser("admin", help="admin page + automatic scheduler")
    admin_p.add_argument("--port", type=int, default=8100)
    run_p = sub.add_parser("run", help="run the follow-up once now")
    run_p.add_argument("--env", choices=["prod", "test"], default="test")
    run_p.add_argument("--mode", choices=["preview", "dry", "live"], default="preview")
    sub.add_parser("scheduler", help="automatic scheduler without the web page")
    sub.add_parser("copy-prod-to-test", help="refresh test data from production")
    sub.add_parser("setup-tabs", help="create missing tracking tabs in the Google Sheet")
    sub.add_parser("build-phone-master", help="create Phone_Master from Master")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    if args.command == "admin":
        code = cmd_admin(args.port)
    elif args.command == "run":
        code = cmd_run(args.env, args.mode)
    elif args.command == "scheduler":
        code = cmd_scheduler()
    elif args.command == "copy-prod-to-test":
        code = cmd_copy_prod_to_test()
    elif args.command == "setup-tabs":
        code = cmd_setup_tabs(Settings.from_env())
    else:
        code = cmd_build_phone_master(Settings.from_env())
    flush()  # send queued LangSmith traces before exiting
    sys.exit(code)
