"""Vendors on the dashboard: which ones the follow-up runs for, their Scenario 1 / 2 / 3 numbers (Phone_Master) and
their Scenario 4 e-mail ids (followup.vendor_contacts, used for the management meeting invite).

Selection: the chosen vendor codes and the scope (selected only / all vendors) are settings (`vendor_codes`,
`vendor_scope`), read by every run (config.Settings.vendor_filter).

Numbers: Phone_Master lives in the Google Sheet, so an edit is written to the sheet first (users keep editing it
there too) and then to the database copy, so the dashboard and the next run use it at once. A blank Scenario number
means "use Master's numbers", exactly as the run does (domain.escalation.apply_phone_master).

Bulk: rows from an uploaded CSV / pasted from Excel. Every row is checked first; nothing is written if any is wrong.
"""

import os
import re

from backend.po_followup.db import store
from backend.po_followup.domain.escalation import indian_mobile, norm, split_numbers, txt
from backend.po_followup.services import followup as service
from backend.po_followup.services import trace

TITLE = "Phone_Master"
# Phone_Master's headers as cli build-phone-master creates them (named in error messages)
DEFAULT_HEADERS = {"code": "Vendor Code", "name": "Vendor Name", 1: "Scenario 1 No", 2: "Scenario 2 No", 3: "Scenario 3 No"}
MAX_ERRORS_SHOWN = 15


class SheetWriteFailed(RuntimeError):
    """The Google Sheet could not be updated; nothing was changed."""


def find_headers(headers: list[str]) -> dict:
    """Which Phone_Master header holds the vendor code, name and Scenario 1/2/3 numbers (first match wins)."""
    found: dict = {}
    for h in headers:
        k = norm(h)
        role = ("code" if k == "vendor code" else "name" if k == "vendor name" else
                next((n for n in (1, 2, 3) if re.fullmatch(rf"scenario {n}( .*)?", k)), None))
        if role is not None:
            found.setdefault(role, h)
    return found


def check_numbers(raw: str, n: int) -> str:
    """One Scenario cell: blank, or one or more 10-digit Indian mobile numbers separated by , ; or /.
    Returns them as 10 digits each."""
    out = []
    for p in split_numbers(raw):
        m = indian_mobile(p)
        if not m:
            raise ValueError(f"Scenario {n}: '{p}' is not a 10-digit Indian mobile number")
        out.append(m)
    return ", ".join(out)


EMAIL_RE = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}")


def check_emails(raw: str) -> str:
    """Scenario 4 cell: blank, or e-mail ids separated by , ; or whitespace. Returns them tidied, lower-cased, de-duplicated."""
    parts = [p for p in re.split(r"[,;\s]+", raw.strip()) if p]
    for p in parts:
        if not EMAIL_RE.fullmatch(p):
            raise ValueError(f"Scenario 4: '{p}' is not an e-mail id")
    return ", ".join(dict.fromkeys(p.lower() for p in parts))


def _fallbacks(vendor: dict | None) -> list[str]:
    """Master's numbers, used when a Scenario cell is blank."""
    if not vendor:
        return ["", "", ""]
    c = trace.contacts(vendor, [], "")
    return [c["scenario1"], c["scenario2"], c["scenario3"]]


def _master_emails(vendor: dict | None) -> str:
    """Buyer / buyer lead / SCM head e-mail ids that Master already gives the meeting."""
    c = (vendor or {}).get("contacts") or {}
    return ", ".join(dict.fromkeys(e for e in (c.get("buyerEmail"), c.get("buyerLeadEmail"), c.get("scmHeadEmail")) if e))


def _cell(pm: dict | None, header: str | None) -> str:
    v = txt((pm or {}).get(header, "")) if header else ""
    return "" if v == "#N/A" else v


def _load() -> dict:
    """Everything the list and the saves need: headers, Phone_Master rows, Master vendors, settings."""
    headers = store.source_headers(TITLE)
    cols = find_headers(headers)
    phone: dict[str, dict] = {}
    for p in (trace.source_records(TITLE) if headers else []):
        code = _cell(p, cols.get("code"))
        if code:
            phone.setdefault(code, p)
    settings = service.get_settings()
    return {"cols": cols, "phone": phone, "settings": settings, "selected": trace.chosen_codes(settings),
            "emails": store.vendor_emails(),
            "vendors": trace.index_vendors(trace.source_records("Master"))}


def _row(code: str, d: dict) -> dict:
    pm, v, cols = d["phone"].get(code), d["vendors"].get(code), d["cols"]
    return {
        "code": code,
        "name": _cell(pm, cols.get("name")) or (v["name"] if v else code),
        "in_phone_master": pm is not None,
        "in_master": v is not None,
        "overdue_lines": len(v["lines"]) if v else 0,
        "selected": code in d["selected"],
        "numbers": [_cell(pm, cols.get(n)) for n in (1, 2, 3)],
        "fallback": _fallbacks(v),
        "emails": d["emails"].get(code, ""),
        "master_emails": _master_emails(v),
    }


def list_numbers() -> dict:
    """Every vendor in Phone_Master or Master; selected vendors first, then those with overdue lines."""
    d = _load()
    codes = list(d["phone"]) + [c for c in d["vendors"] if c not in d["phone"]]
    rows = [_row(c, d) for c in codes]
    s = d["settings"]
    return {
        "ready": "code" in d["cols"],
        "scope": s["vendor_scope"],
        # selected codes that are in neither Master nor Phone_Master (typos, vendors gone from the sheet)
        "unknown_selected": sorted(d["selected"] - set(codes)),
        "vendors": sorted(rows, key=lambda r: (not r["selected"], -r["overdue_lines"], r["name"].lower(), r["code"])),
    }


# ---------- changes ----------

def _plan(d: dict, items: list[dict]) -> dict:
    """Check every item and work out what changes. Item: {code, name?, numbers?: [s1|None, s2|None, s3|None],
    follow?: bool|None, line?: row number in the upload}. None = leave as it is."""
    cols = d["cols"]
    if "code" not in cols:
        raise ValueError(f"{TITLE} has no 'Vendor Code' column")
    errors, seen = [], set()
    writes, select_add, select_remove, unchanged = [], set(), set(), 0
    email_writes: dict[str, str] = {}
    for it in items:
        code = txt(it.get("code"))
        where = f"Row {it['line']}" if it.get("line") else "Vendor"
        label = f"{where} ({code})" if code else where
        if not code:
            errors.append(f"{where}: vendor code is missing")
            continue
        if code in seen:
            errors.append(f"{label}: this vendor is listed twice")
            continue
        seen.add(code)
        if code not in d["phone"] and code not in d["vendors"] and not txt(it.get("name")):
            errors.append(f"{label}: new vendor, so the vendor name is needed")
            continue
        current = _row(code, d)

        numbers = list(it.get("numbers") or [None, None, None]) + [None] * 3
        new_numbers = list(current["numbers"])
        for n in (1, 2, 3):
            raw = numbers[n - 1]
            if raw is None:
                continue
            if n not in cols:
                errors.append(f"{TITLE} has no column for Scenario {n}; add a '{DEFAULT_HEADERS[n]}' column to the sheet")
                continue
            try:
                new_numbers[n - 1] = check_numbers(str(raw), n)
            except ValueError as e:
                errors.append(f"{label}: {e}")

        new_emails = None
        if it.get("emails") is not None:
            try:
                new_emails = check_emails(str(it["emails"]))
            except ValueError as e:
                errors.append(f"{label}: {e}")
        if new_emails is not None and new_emails != current["emails"]:
            email_writes[code] = new_emails

        name = txt(it.get("name"))
        row = {cols["code"]: code}
        row.update({cols[n]: new_numbers[n - 1] for n in (1, 2, 3)
                    if n in cols and numbers[n - 1] is not None and new_numbers[n - 1] != current["numbers"][n - 1]})
        if "name" in cols and name and name != current["name"]:
            row[cols["name"]] = name
        elif "name" in cols and not current["in_phone_master"] and len(row) > 1:
            row[cols["name"]] = current["name"]  # a new Phone_Master row gets Master's name
        if len(row) > 1:
            writes.append(row)

        follow = it.get("follow")
        if follow is True and not current["selected"]:
            select_add.add(code)
        elif follow is False and current["selected"]:
            select_remove.add(code)
        if len(row) == 1 and code not in select_add | select_remove | email_writes.keys():
            unchanged += 1

    if errors:
        errors = list(dict.fromkeys(errors))
        more = len(errors) - MAX_ERRORS_SHOWN
        raise ValueError("Nothing was saved. " + "; ".join(errors[:MAX_ERRORS_SHOWN])
                         + (f"; and {more} more problem(s)" if more > 0 else ""))
    added = sum(1 for r in writes if r[cols["code"]] not in d["phone"])
    return {"writes": writes, "select_add": select_add, "select_remove": select_remove, "email_writes": email_writes,
            "summary": {"numbers_updated": len(writes) - added, "numbers_added": added, "emails_updated": len(email_writes),
                        "selected_added": sorted(select_add), "selected_removed": sorted(select_remove),
                        "unchanged": unchanged}}


def _apply(d: dict, plan: dict) -> None:
    if plan["writes"]:
        _write_sheet(plan["writes"], d["cols"]["code"])
        store.upsert_source_rows(TITLE, plan["writes"], d["cols"]["code"])
        trace.clear_cache()
    if plan["email_writes"]:
        store.save_vendor_emails(plan["email_writes"])
    if plan["select_add"] or plan["select_remove"]:
        _save_selection((d["selected"] | plan["select_add"]) - plan["select_remove"])


def save_one(code: str, name: str | None, numbers: list[str], emails: str | None = None) -> dict:
    """Set one vendor's Scenario 1/2/3 numbers and Scenario 4 e-mail ids (adds the vendor to Phone_Master if it is
    not there yet)."""
    code = code.strip()
    d = _load()
    _apply(d, _plan(d, [{"code": code, "name": name, "numbers": numbers, "emails": emails}]))
    return _row(code, _load())


def bulk(rows: list[dict], apply: bool) -> dict:
    """Numbers and/or Follow up for many vendors. apply=False only checks the rows and says what would change."""
    d = _load()
    plan = _plan(d, rows)
    if apply:
        _apply(d, plan)
    return {"applied": apply, **plan["summary"]}


def set_selection(codes: list[str], scope: str, confirm: str | None) -> dict:
    """Which vendors runs follow up: `codes` with scope 'selected', or every vendor with scope 'all'."""
    d = _load()
    codes_set = {c.strip() for c in codes if c.strip()}
    unknown = sorted(c for c in codes_set - d["selected"] if c not in d["phone"] and c not in d["vendors"])
    if unknown:
        raise ValueError(f"Not found in Master or Phone_Master: {', '.join(unknown[:10])}")
    if scope == "all" and d["settings"]["vendor_scope"] != "all" and confirm != "ALL":
        raise ValueError("Following up every vendor needs confirm = ALL")
    store.save_settings(service.validate_settings({"vendor_scope": scope}))
    _save_selection(codes_set)
    s = service.get_settings()
    return {"scope": s["vendor_scope"], "selected": sorted(trace.chosen_codes(s))}


def _save_selection(codes: set[str]) -> None:
    store.save_settings(service.validate_settings({"vendor_codes": ",".join(sorted(codes))}))


def _write_sheet(rows: list[dict], key: str) -> None:
    from backend.po_followup.integrations.sheets import PoSheet

    try:
        PoSheet(os.getenv("PO_SHEET_ID", "")).upsert_rows(TITLE, rows, key)
    except Exception as e:  # not configured, no access, network: say so and change nothing
        raise SheetWriteFailed(f"Could not save to the Google Sheet: {e}") from e
