"""Vendors on the dashboard: which ones the follow-up runs for, and their Scenario 1 / 2 / 3 numbers (Phone_Master).

Selection: the selected vendor codes and the scope (selected only / all vendors) are environment settings
(`pilot_vendor_codes`, `vendor_scope`), read by every run (config.Settings.vendor_filter).

Numbers: production's Phone_Master lives in the Google Sheet, so an edit is written to the sheet first (users keep
editing it there too) and then to the production copy, so the dashboard and the next run use it at once.
Test: written to the test database only. A blank Scenario number means "use Master's numbers", exactly as the
run does (domain.escalation.apply_phone_master).

Bulk: rows from an uploaded CSV / pasted from Excel. Every row is checked first; nothing is written if any is wrong.
"""

import os
import re

from backend.po_followup.domain.escalation import is_valid_mobile, normalize_mobile, norm, split_numbers, txt
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
    """One Scenario cell: blank, or one or more mobile numbers separated by , ; or /. Returns it tidied."""
    parts = split_numbers(raw)
    for p in parts:
        if not is_valid_mobile(normalize_mobile(p, False, "")):
            raise ValueError(f"Scenario {n}: '{p}' is not a mobile number (10 digits, or with country code)")
    return ", ".join(parts)


def _fallbacks(vendor: dict | None) -> list[str]:
    """Master's numbers, used when a Scenario cell is blank."""
    if not vendor:
        return ["", "", ""]
    c = trace.contacts(vendor, [], "")
    return [c["scenario1"], c["scenario2"], c["scenario3"]]


def _cell(pm: dict | None, header: str | None) -> str:
    v = txt((pm or {}).get(header, "")) if header else ""
    return "" if v == "#N/A" else v


def _load(env: str) -> dict:
    """Everything the list and the saves need: headers, Phone_Master rows, Master vendors, settings."""
    from backend.po_followup.db import store
    from backend.po_followup.services import followup as service

    headers = store.source_headers(env, TITLE)
    cols = find_headers(headers)
    phone: dict[str, dict] = {}
    for p in (trace.source_records(env, TITLE) if headers else []):
        code = _cell(p, cols.get("code"))
        if code:
            phone.setdefault(code, p)
    settings = service.get_settings(env)
    return {"cols": cols, "phone": phone, "settings": settings, "selected": trace._pilot_codes(settings),
            "vendors": trace.index_vendors(trace.source_records(env, "Master"))}


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
    }


def list_numbers(env: str) -> dict:
    """Every vendor in Phone_Master or Master; selected vendors first, then those with overdue lines."""
    d = _load(env)
    codes = list(d["phone"]) + [c for c in d["vendors"] if c not in d["phone"]]
    rows = [_row(c, d) for c in codes]
    s = d["settings"]
    return {
        "env": env,
        "saves_to": "Google Sheet (Phone_Master)" if env == "prod" else "test database",
        "ready": "code" in d["cols"],
        "scope": s["vendor_scope"],
        "pilot_mode": bool(s["pilot_mode"]),
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
        if code not in d["phone"] and code not in d["vendors"]:
            errors.append(f"{label}: vendor code not found in Master or Phone_Master")
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
        if len(row) == 1 and code not in select_add | select_remove:
            unchanged += 1

    if errors:
        errors = list(dict.fromkeys(errors))
        more = len(errors) - MAX_ERRORS_SHOWN
        raise ValueError("Nothing was saved. " + "; ".join(errors[:MAX_ERRORS_SHOWN])
                         + (f"; and {more} more problem(s)" if more > 0 else ""))
    added = sum(1 for r in writes if r[cols["code"]] not in d["phone"])
    return {"writes": writes, "select_add": select_add, "select_remove": select_remove,
            "summary": {"numbers_updated": len(writes) - added, "numbers_added": added,
                        "selected_added": sorted(select_add), "selected_removed": sorted(select_remove),
                        "unchanged": unchanged}}


def _apply(env: str, d: dict, plan: dict) -> None:
    from backend.po_followup.db import store

    if plan["writes"]:
        if env == "prod":
            _write_sheet(plan["writes"], d["cols"]["code"])
        store.upsert_source_rows(env, TITLE, plan["writes"], d["cols"]["code"])
        trace.clear_cache()
    if plan["select_add"] or plan["select_remove"]:
        _save_selection(env, (d["selected"] | plan["select_add"]) - plan["select_remove"])


def save_one(env: str, code: str, name: str | None, numbers: list[str]) -> dict:
    """Set one vendor's Scenario 1/2/3 numbers (adds the vendor to Phone_Master if it is not there yet)."""
    code = code.strip()
    d = _load(env)
    _apply(env, d, _plan(d, [{"code": code, "name": name, "numbers": numbers}]))
    return _row(code, _load(env))


def bulk(env: str, rows: list[dict], apply: bool) -> dict:
    """Numbers and/or Follow up for many vendors. apply=False only checks the rows and says what would change."""
    d = _load(env)
    plan = _plan(d, rows)
    if apply:
        _apply(env, d, plan)
    return {"applied": apply, **plan["summary"]}


def set_selection(env: str, codes: list[str], scope: str, confirm: str | None) -> dict:
    """Which vendors runs follow up: `codes` with scope 'selected', or every vendor with scope 'all'."""
    from backend.po_followup.db import store
    from backend.po_followup.services import followup as service

    d = _load(env)
    codes_set = {c.strip() for c in codes if c.strip()}
    unknown = sorted(c for c in codes_set - d["selected"] if c not in d["phone"] and c not in d["vendors"])
    if unknown:
        raise ValueError(f"Not found in Master or Phone_Master: {', '.join(unknown[:10])}")
    if env == "prod" and scope == "all" and d["settings"]["vendor_scope"] != "all" and confirm != "ALL":
        raise ValueError("Following up every production vendor needs confirm = ALL")
    store.save_settings(env, service.validate_settings({"vendor_scope": scope}))
    _save_selection(env, codes_set)
    s = service.get_settings(env)
    return {"scope": s["vendor_scope"], "selected": sorted(trace._pilot_codes(s))}


def _save_selection(env: str, codes: set[str]) -> None:
    from backend.po_followup.db import store
    from backend.po_followup.services import followup as service

    store.save_settings(env, service.validate_settings({"pilot_vendor_codes": ",".join(sorted(codes))}))


def _write_sheet(rows: list[dict], key: str) -> None:
    from backend.po_followup.integrations.sheets import PoSheet

    try:
        PoSheet(os.getenv("PO_SHEET_ID", "")).upsert_rows(TITLE, rows, key)
    except Exception as e:  # not configured, no access, network: say so and change nothing
        raise SheetWriteFailed(f"Could not save to the Google Sheet: {e}") from e
