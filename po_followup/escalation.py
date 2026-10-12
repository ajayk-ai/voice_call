"""Escalation rules, ported from the n8n workflow 'scm po follow up agent'.

Pure functions only (no I/O), so the rules can be tested without Google Sheets or WhatsApp.

Rules:
- A Master row is PENDING when OD Days > 0. Each row (PO / item / schedule line) has its own
  escalation state in Line_Tracker, keyed by "PO/item/schedule line".
- A pending line moves one level up once the interval for its current level has passed
  (default 1 day each, set per environment in the admin page):
  0 vendor alert -> 1 vendor management -> 2 buyer call task -> 3 management meeting.
- Lines are grouped per vendor and level: one message per vendor per level per day.
- A tracked line that is no longer pending in Master is CLEARED.
"""

import math
import re
from datetime import date, datetime, timedelta

ALERT_TYPES = ["VENDOR_ALERT_DAY0", "ESCALATION_DAY1", "MGMT_ESCALATION_DAY2", "MGMT_MEETING_DAY3"]
MAX_LEVEL = 3
# Minutes to wait after level 0, 1 and 2 before escalating to the next level
DEFAULT_INTERVALS_MINUTES = (1440, 1440, 1440)

ALIASES = {
    "material": ["Material", "Material Code", "Item Code", "Part No"],
    "materialDescription": ["Mat. Desc", "Mat Desc", "Material Description", "Description"],
    "vendorCode": ["Vendor Code", "Supplier Code", "Vendor ID"],
    "vendorName": ["Vendor Name", "Supplier Name", "Vendor"],
    "poNumber": ["PO No", "PO Number", "PO", "Purchase Order", "Purchase Order Number"],
    "poItem": ["PO Item No", "PO Item", "Item No"],
    "schedLine": ["Del.Sch Line", "Del Sch Line", "Schedule Line", "Sch Line"],
    "poDate": ["Purchase Order Date", "PO Date", "Doc Date"],
    "uom": ["UOM", "Unit"],
    "pendingQty": ["Pending Qty", "Pending Quantity", "Open Qty"],
    "odDays": ["OD Days", "Overdue Days", "Days Overdue"],
    "purchasingGroup": ["Purchasing Group", "PG"],
    "vendorCoordMobile": ["vendor coordinator number", "Vendor Coordinator Mobile", "Vendor Mobile"],
    "vendorPlannerMobile": ["vendor planner number", "Vendor Planner Mobile"],
    "vendorMgmtMobile": ["vendor management number", "Vendor Management Mobile"],
    "buyerMobile": ["buyer number", "Buyer Mobile"],
    "buyerEmail": ["Buyer Mail ID", "Buyer Email"],
    "buyerLeadEmail": ["Buyer Lead Mail Id", "Buyer Lead Email"],
    "scmHeadEmail": ["SCM Head Mail id", "SCM Head Email"],
}
CONTACT_FIELDS = [
    "vendorCoordMobile", "vendorPlannerMobile", "vendorMgmtMobile", "buyerMobile",
    "buyerEmail", "buyerLeadEmail", "scmHeadEmail", "purchasingGroup",
]
DATE_FORMATS = [
    "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y",
    "%d-%m-%y", "%d/%m/%y", "%Y-%m-%d %H:%M:%S",
]
SHEETS_EPOCH = date(1899, 12, 30)


# ---------- small helpers ----------

def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s if s is not None else "").lower()).strip()


def txt(v) -> str:
    return "" if v is None else str(v).strip()


def num(v) -> float | None:
    s = txt(v).replace(",", "")
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def fmt_num(n: float) -> int | float:
    """12.0 -> 12 (how the n8n/JavaScript version printed numbers)."""
    return int(n) if float(n).is_integer() else n


def parse_date(v) -> date | None:
    s = txt(v)
    if not s:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", s):  # Google Sheets serial date
        return SHEETS_EPOCH + timedelta(days=int(float(s)))
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def pretty(d: date | None) -> str:
    return d.strftime("%d-%b-%Y") if d else ""


def line_key(po: str, item: str, sched: str) -> str:
    return "/".join([po, item or "0", sched or "1"])


def split_numbers(raw: str) -> list[str]:
    return [s.strip() for s in re.split(r"[,;/]", txt(raw)) if s.strip()]


_ALIAS_INDEX = {norm(name): field for field, names in ALIASES.items() for name in names}


def map_row(record: dict) -> dict:
    """Map a sheet row (any of the known header spellings) to canonical field names."""
    r: dict = {}
    for k, v in record.items():
        field = _ALIAS_INDEX.get(norm(k))
        if field and txt(r.get(field)) == "":
            r[field] = v
    return r


def last_alert_time(t: dict, tz) -> datetime | None:
    """'Last Alert At' (timestamp) if present, else midnight of 'Last Alert Date' (rows written by n8n)."""
    at = txt(t.get("Last Alert At"))
    if at:
        try:
            return datetime.strptime(at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except ValueError:
            pass
    d = parse_date(t.get("Last Alert Date"))
    return datetime(d.year, d.month, d.day, tzinfo=tz) if d else None


def interval_elapsed(last: datetime | None, now: datetime, minutes: float) -> bool:
    """True once `minutes` have passed since `last`. A little slack (10%, at most 5 min) so a daily run
    at 08:30 still escalates a line alerted at 08:30:05 the day before."""
    if last is None:
        return True
    need = timedelta(minutes=minutes)
    return now - last >= need - min(timedelta(minutes=5), need * 0.1)


def tracker_index(tracker_records: list[dict]) -> dict[str, dict]:
    return {txt(t.get("Line Key")): t for t in tracker_records if txt(t.get("Line Key"))}


# ---------- 03 Build Pending Lines & Escalation Groups ----------

def build_groups(master_records: list[dict], tracker_records: list[dict], now: datetime, max_lines: int = 15,
                 intervals_minutes: tuple = DEFAULT_INTERVALS_MINUTES) -> list[dict]:
    run_date = now.date()
    run_iso = run_date.isoformat()
    tracker = tracker_index(tracker_records)
    vendor_info: dict[str, dict] = {}
    groups: dict[tuple, dict] = {}
    seen_keys: set[str] = set()

    for record in master_records:
        r = map_row(record)
        po = txt(r.get("poNumber"))
        vendor_code = txt(r.get("vendorCode")) or txt(r.get("vendorName"))
        if not po or not vendor_code:
            continue

        vi = vendor_info.setdefault(vendor_code, {"vendorName": txt(r.get("vendorName")) or vendor_code, "contacts": {}})
        for c in CONTACT_FIELDS:
            value = txt(r.get(c))
            if not vi["contacts"].get(c) and value and value != "#N/A":
                vi["contacts"][c] = value

        od = num(r.get("odDays"))
        if not (od is not None and od > 0):  # PENDING RULE: OD Days > 0
            continue
        key = line_key(po, txt(r.get("poItem")), txt(r.get("schedLine")))
        if key in seen_keys:
            continue
        seen_keys.add(key)

        t = tracker.get(key)
        active = bool(t) and not re.search(r"CLEARED", txt(t.get("Status")), re.I)
        level_match = re.search(r"\d+", txt(t.get("Escalation Level"))) if active else None
        current_level = int(level_match.group()) if level_match else -1
        last_alert = txt(t.get("Last Alert Date")) if active else ""
        next_level = current_level + 1
        if next_level > MAX_LEVEL:
            continue  # meeting stage already reached
        if current_level >= 0 and not interval_elapsed(last_alert_time(t, now.tzinfo), now, intervals_minutes[current_level]):
            continue  # waiting for this level's interval

        due = run_date - timedelta(days=od)  # Del.Date cells are unreliable (day/month swaps)
        g = groups.setdefault((vendor_code, next_level), {"vendorCode": vendor_code, "nextLevel": next_level, "lines": []})
        g["lines"].append({
            "key": key, "poNumber": po, "poItem": txt(r.get("poItem")), "schedLine": txt(r.get("schedLine")),
            "desc": txt(r.get("materialDescription")) or txt(r.get("material")), "material": txt(r.get("material")),
            "pending": txt(r.get("pendingQty")), "uom": txt(r.get("uom")), "od": fmt_num(od),
            "due": due, "dueText": pretty(due), "poDate": parse_date(r.get("poDate")),
            "firstAlertDate": (txt(t.get("First Alert Date")) if active else "") or run_iso,
            "lastAlertType": txt(t.get("Last Alert Type")) if active else "",
            "lastAlertDate": last_alert,
        })

    out = []
    for g in groups.values():
        vi = vendor_info[g["vendorCode"]]
        c = vi["contacts"]
        lines = sorted(g["lines"], key=lambda l: -l["od"])
        pos = list(dict.fromkeys(l["poNumber"] for l in lines))
        shown = lines[:max_lines]
        more = len(lines) - len(shown)
        summary = "\n".join(
            f"{i + 1}. PO {l['poNumber']}{'/' + l['poItem'] if l['poItem'] else ''} | {l['desc'][:35]} | "
            f"Due {l['dueText']} | Pend {l['pending']} {l['uom']} | {l['od']}d late"
            for i, l in enumerate(shown)
        ) + (f"\n(+{more} more pending lines)" if more > 0 else "")
        first_due = min(l["due"] for l in lines)
        po_dates = sorted(l["poDate"] for l in lines if l["poDate"])
        out.append({
            "runDate": run_iso,
            "vendorCode": g["vendorCode"],
            "vendorName": vi["vendorName"],
            "vendorLabel": f"{vi['vendorName']} ({g['vendorCode']})",
            "poNumber": ", ".join(pos[:25]) + (" …" if len(pos) > 25 else ""),
            "poCount": len(pos),
            "overdueLines": len(lines),
            "daysOverdue": lines[0]["od"],
            "lineSummary": summary,
            "material": "; ".join(f"{l['material']} {l['desc']}" for l in shown[:5]),
            "expectedDisplay": pretty(first_due),
            "poDateDisplay": pretty(po_dates[0]) if po_dates else "",
            "issueSummary": f"{len(lines)} pending PO line(s) (OD Days > 0) across {len(pos)} PO(s)",
            "purchasingGroup": c.get("purchasingGroup", ""),
            "vendorCoordMobileRaw": c.get("vendorCoordMobile", ""),
            "vendorPlannerMobileRaw": c.get("vendorPlannerMobile", ""),
            "vendorMgmtMobileRaw": c.get("vendorMgmtMobile", ""),
            "buyerMobileRaw": c.get("buyerMobile", ""),
            "buyerEmail": c.get("buyerEmail", ""),
            "buyerLeadEmail": c.get("buyerLeadEmail", ""),
            "scmHeadEmail": c.get("scmHeadEmail", ""),
            "buyerName": c["buyerEmail"].split("@")[0] if c.get("buyerEmail") else f"Buyer (PG {c.get('purchasingGroup') or '-'})",
            "nextLevel": g["nextLevel"],
            "lastAlertType": lines[0]["lastAlertType"],
            "previousAlertDate": lines[0]["lastAlertDate"],
            "lines": [
                {k: l[k] for k in ("key", "poNumber", "poItem", "schedLine", "material", "desc", "pending", "uom",
                                   "dueText", "od", "firstAlertDate")}
                for l in lines
            ],
        })
    return out


# ---------- 03c Detect Cleared Lines ----------

def detect_cleared(master_records: list[dict], tracker_records: list[dict], run_date: date, timestamp: str) -> list[dict]:
    """Tracked lines that are no longer pending in Master -> CLEARED updates for Line_Tracker."""
    pending_keys = set()
    master_rows = 0
    for record in master_records:
        r = map_row(record)
        po = txt(r.get("poNumber"))
        if not po:
            continue
        master_rows += 1
        od = num(r.get("odDays"))
        if od is not None and od > 0:
            pending_keys.add(line_key(po, txt(r.get("poItem")), txt(r.get("schedLine"))))
    if master_rows == 0:
        return []  # safety: never clear everything on an empty / failed Master read

    out = []
    for t in tracker_records:
        k = txt(t.get("Line Key"))
        if not k or re.search(r"CLEARED", txt(t.get("Status")), re.I):
            continue
        if k not in pending_keys:
            out.append({"Line Key": k, "Status": "CLEARED", "Cleared Date": run_date.isoformat(), "Updated At": timestamp})
    return out


# ---------- 03f Apply Phone Master ----------

def _clean(v) -> str:
    s = txt(v)
    return "" if s == "#N/A" else s


def scenario_number(phone_row: dict, n: int) -> str:
    """'Scenario 1 No' or 'Scenario 1 mobile No' (the live sheet uses the latter) - any header
    starting with 'scenario <n>'."""
    for k, v in phone_row.items():
        if re.fullmatch(rf"scenario {n}( .*)?", norm(k)):
            return _clean(v)
    return ""


def apply_phone_master(groups: list[dict], phone_records: list[dict]) -> list[dict]:
    """Phone_Master (one row per Vendor Code) overrides Master numbers; blank cells fall back to Master.
    Scenario 1 = Day 0 contacts, Scenario 2 = Day 1 management, Scenario 3 = Day 2 call number."""
    pm = {}
    for p in phone_records:
        code = txt(next((v for k, v in p.items() if norm(k) == "vendor code"), ""))
        if code:
            pm[code] = p
    out = []
    for g in groups:
        m = pm.get(txt(g["vendorCode"]), {})
        pm1, pm2, pm3 = (scenario_number(m, n) for n in (1, 2, 3))
        s1 = pm1 or ",".join(x for x in (_clean(g["vendorPlannerMobileRaw"]), _clean(g["vendorCoordMobileRaw"])) if x)
        s2 = pm2 or _clean(g["vendorMgmtMobileRaw"])
        s3 = pm3 or s2
        first_s1 = (split_numbers(s1) or [""])[0]
        out.append({
            **g,
            "scenario1Raw": s1, "scenario2Raw": s2, "scenario3Raw": s3,
            "phoneSource": "Phone_Master" if (pm1 or pm2 or pm3) else "Master (fallback)",
            "vendorPlannerMobileRaw": first_s1 or g["vendorPlannerMobileRaw"],
            "vendorMgmtMobileRaw": s3,
        })
    return out


# ---------- 05 / 06 / 06b / 06c: decide what goes out today ----------

def alert_type(level: int) -> str:
    return ALERT_TYPES[level] if 0 <= level < len(ALERT_TYPES) else "MAX_LEVEL_REACHED"


def alert_key(vendor_code: str, level: int, run_iso: str) -> str:
    return f"{vendor_code}|L{level}|{alert_type(level) if level <= MAX_LEVEL else 'MAX'}|{run_iso}"


def select_alerts(groups: list[dict], sent_keys_today: set[str], pilot_mode: bool, pilot_vendor_codes: set[str]) -> list[dict]:
    """Attach alertType/alertKey, then drop: levels out of range, keys already in today's Alert_Log,
    repeat vendor+level groups, and (in pilot mode) vendors not in the pilot list."""
    seen = set()
    out = []
    for g in groups:
        level = g["nextLevel"]
        if not 0 <= level <= MAX_LEVEL:
            continue
        g = {**g, "alertType": alert_type(level), "alertKey": alert_key(g["vendorCode"], level, g["runDate"])}
        if g["alertKey"] in sent_keys_today:
            continue
        if (g["vendorCode"], level) in seen:
            continue
        seen.add((g["vendorCode"], level))
        if pilot_mode and txt(g["vendorCode"]) not in pilot_vendor_codes:
            continue
        out.append(g)
    return out


# ---------- recipients (08a / 08b) ----------

def recipients_from(raw: str, role: str, vendor_name: str, label: str, fallback_name: str) -> list[dict]:
    """Several numbers in one cell (comma/semicolon/slash), de-duplicated by digits.
    Always returns at least one recipient, so a missing number is logged rather than silently skipped."""
    seen = set()
    out = []
    for i, n in enumerate(split_numbers(raw)):
        digits = re.sub(r"\D", "", n)
        if not digits or digits in seen:
            continue
        seen.add(digits)
        out.append({"role": role, "name": f"{vendor_name} ({label} {i + 1})", "rawMobile": n})
    return out or [{"role": role, "name": fallback_name, "rawMobile": ""}]


def normalize_mobile(raw: str, pilot_mode: bool, pilot_mobile: str) -> str:
    """First number in the cell, digits only, no leading zeros, 10-digit Indian numbers get 91."""
    source = pilot_mobile if pilot_mode else raw
    digits = re.sub(r"\D", "", (split_numbers(str(source or "")) or [""])[0]).lstrip("0")
    return "91" + digits if len(digits) == 10 else digits


def is_valid_mobile(mobile: str) -> bool:
    return re.fullmatch(r"[1-9]\d{10,14}", mobile) is not None
