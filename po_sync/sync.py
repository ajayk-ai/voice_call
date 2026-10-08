"""Live mirror of every tab of the PO Google Sheet into its own PostgreSQL database.

Auth: a GCP service account with domain-wide delegation, impersonating GOOGLE_DELEGATED_USER.
Each tab becomes a table in the `sheets` schema (all columns TEXT, plus `sheet_row`), and
public.sync_state records what was synced and when. Every cycle reads all tabs in 2 API calls
and rewrites only the tabs whose content changed, each in its own transaction, so readers
always see a complete, consistent copy of a tab.

Run:  python -m po_sync          # keep syncing every PO_SYNC_INTERVAL_SECONDS
      python -m po_sync --once   # one sync, then exit
"""

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sys
import threading
from dataclasses import dataclass

import truststore

# Use the OS certificate store, so HTTPS works behind corporate SSL inspection (e.g. Sophos)
truststore.inject_into_ssl()

import gspread
import psycopg
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from psycopg import sql

load_dotenv()

log = logging.getLogger("po_sync")

# Must match a scope authorized for this client in Workspace Admin > Domain-wide delegation,
# otherwise Google returns "unauthorized_client". This code only reads the sheet.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SCHEMA = "sheets"
MAX_IDENTIFIER = 63  # PostgreSQL identifier length limit
MAX_BACKOFF_SECONDS = 15 * 60


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing {name} in .env (see .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    service_account_file: str
    delegated_user: str
    sheet_id: str
    interval_seconds: int
    db: dict

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            service_account_file=require_env("GOOGLE_SERVICE_ACCOUNT_FILE"),
            delegated_user=require_env("GOOGLE_DELEGATED_USER"),
            sheet_id=require_env("PO_SHEET_ID"),
            interval_seconds=int(os.getenv("PO_SYNC_INTERVAL_SECONDS", "60")),
            db={
                "host": os.getenv("PO_DB_HOST", "localhost"),
                "port": int(os.getenv("PO_DB_PORT", "5432")),
                "user": require_env("PO_DB_USER"),
                "password": os.getenv("PO_DB_PASSWORD", ""),
                "dbname": os.getenv("PO_DB_NAME", "pending_po"),
            },
        )


# ---------- naming ----------

def slugify(text: str, fallback: str) -> str:
    """'Mat. Desc' -> 'mat_desc', 'OD Days\\r' -> 'od_days', '' -> fallback."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")
    if not slug:
        slug = fallback
    if slug[0].isdigit():
        slug = "c_" + slug
    return slug[:MAX_IDENTIFIER]


def unique_names(raw_names: list[str], fallback_prefix: str, reserved: set[str] = frozenset()) -> list[str]:
    """Slugify each name and make the results unique: mail_id, mail_id_2, ..."""
    seen = set(reserved)
    out = []
    for i, raw in enumerate(raw_names, start=1):
        base = slugify(raw, f"{fallback_prefix}{i}")
        name, n = base, 2
        while name in seen:
            suffix = f"_{n}"
            name = base[: MAX_IDENTIFIER - len(suffix)] + suffix
            n += 1
        seen.add(name)
        out.append(name)
    return out


# ---------- reading the sheet ----------

@dataclass
class Tab:
    gid: int
    title: str
    table: str
    headers: list[str]  # original header text
    columns: list[str]  # column names in Postgres
    rows: list[tuple[int, list]]  # (sheet row number, values padded to len(columns))
    content_hash: str


def build_tab(gid: int, title: str, table: str, values: list[list[str]]) -> Tab:
    """Turn raw sheet values (first row = headers) into a Tab ready to write."""
    headers = [str(h) for h in values[0]] if values else []
    data = values[1:]
    width = max([len(headers)] + [len(r) for r in data]) if values else 0
    headers += [""] * (width - len(headers))  # data wider than the header row gets col_N columns
    columns = unique_names(headers, "col_", reserved={"sheet_row"})

    rows = []
    for offset, raw in enumerate(data):
        cells = [str(v) if v != "" else None for v in raw] + [None] * (width - len(raw))
        if any(c is not None for c in cells):  # skip fully blank rows
            rows.append((offset + 2, cells))  # +2: 1-based, after the header row

    digest = hashlib.sha256(json.dumps([title, headers, rows], ensure_ascii=False).encode()).hexdigest()
    return Tab(gid, title, table, headers, columns, rows, digest)


def open_sheet(cfg: Config) -> gspread.Spreadsheet:
    creds = Credentials.from_service_account_file(cfg.service_account_file, scopes=SCOPES)
    creds = creds.with_subject(cfg.delegated_user)  # domain-wide delegation
    return gspread.authorize(creds).open_by_key(cfg.sheet_id)


def read_all_tabs(spreadsheet: gspread.Spreadsheet) -> list[Tab]:
    """All tabs in 2 API calls: one for tab metadata, one batch read of every tab's values."""
    worksheets = spreadsheet.worksheets()
    if not worksheets:
        return []
    ranges = ["'" + ws.title.replace("'", "''") + "'" for ws in worksheets]
    result = spreadsheet.values_batch_get(ranges, params={"valueRenderOption": "FORMATTED_VALUE"})
    value_ranges = result.get("valueRanges", [])

    table_names = unique_names([ws.title for ws in worksheets], "tab_", reserved={"sync_state"})
    return [
        build_tab(ws.id, ws.title, table, vr.get("values", []))
        for ws, table, vr in zip(worksheets, table_names, value_ranges)
    ]


# ---------- writing to Postgres ----------

def ensure_database(cfg: Config) -> None:
    """Create the dedicated database if it doesn't exist yet."""
    params = {**cfg.db, "dbname": "postgres"}
    with psycopg.connect(**params, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg.db["dbname"],)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cfg.db["dbname"])))
            log.info("Created database %s", cfg.db["dbname"])


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public.sync_state (
                gid            BIGINT PRIMARY KEY,
                tab_title      TEXT NOT NULL,
                table_name     TEXT NOT NULL,
                columns        JSONB NOT NULL,
                row_count      INTEGER NOT NULL,
                content_hash   TEXT NOT NULL,
                last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_error     TEXT
            )
            """
        )


def load_state(conn: psycopg.Connection) -> dict[int, dict]:
    rows = conn.execute("SELECT gid, table_name, columns, content_hash FROM public.sync_state").fetchall()
    return {gid: {"table": table, "columns": cols, "hash": h} for gid, table, cols, h in rows}


def table_ident(table: str) -> sql.Composed:
    return sql.Identifier(SCHEMA, table)


def write_tab(conn: psycopg.Connection, tab: Tab, previous: dict | None) -> None:
    """Replace one tab's table atomically. Recreates it if the columns or name changed."""
    column_map = dict(zip(tab.columns, tab.headers))
    with conn.transaction():
        if previous and previous["table"] != tab.table:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table_ident(previous["table"])))

        same_shape = previous and previous["table"] == tab.table and list(previous["columns"]) == tab.columns
        if same_shape:
            conn.execute(sql.SQL("TRUNCATE {}").format(table_ident(tab.table)))
        else:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table_ident(tab.table)))
            conn.execute(
                sql.SQL("CREATE TABLE {} (sheet_row INTEGER PRIMARY KEY, {})").format(
                    table_ident(tab.table),
                    sql.SQL(", ").join(sql.SQL("{} TEXT").format(sql.Identifier(c)) for c in tab.columns),
                )
                if tab.columns
                else sql.SQL("CREATE TABLE {} (sheet_row INTEGER PRIMARY KEY)").format(table_ident(tab.table))
            )
            for col, header in column_map.items():
                conn.execute(
                    sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
                        table_ident(tab.table), sql.Identifier(col), sql.Literal(header)
                    )
                )
            conn.execute(
                sql.SQL("COMMENT ON TABLE {} IS {}").format(
                    table_ident(tab.table), sql.Literal(f"Mirror of sheet tab '{tab.title}' (gid {tab.gid})")
                )
            )

        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
            table_ident(tab.table),
            sql.SQL(", ").join(sql.Identifier(c) for c in ["sheet_row", *tab.columns]),
        )
        with conn.cursor().copy(copy_sql) as copy:
            for row_number, cells in tab.rows:
                copy.write_row([row_number, *cells])

        conn.execute(
            """
            INSERT INTO public.sync_state (gid, tab_title, table_name, columns, row_count, content_hash, last_synced_at, last_error)
            VALUES (%s, %s, %s, %s, %s, %s, now(), NULL)
            ON CONFLICT (gid) DO UPDATE SET
                tab_title = EXCLUDED.tab_title, table_name = EXCLUDED.table_name, columns = EXCLUDED.columns,
                row_count = EXCLUDED.row_count, content_hash = EXCLUDED.content_hash,
                last_synced_at = now(), last_error = NULL
            """,
            (tab.gid, tab.title, tab.table, json.dumps(tab.columns), len(tab.rows), tab.content_hash),
        )


def record_error(conn: psycopg.Connection, gid: int, message: str) -> None:
    try:
        with conn.transaction():
            conn.execute("UPDATE public.sync_state SET last_error = %s WHERE gid = %s", (message[:2000], gid))
    except psycopg.Error:
        log.exception("Could not record error for gid %s", gid)


def drop_removed_tabs(conn: psycopg.Connection, state: dict[int, dict], live_gids: set[int]) -> None:
    """A tab deleted from the sheet is dropped from the mirror too."""
    for gid, prev in state.items():
        if gid in live_gids:
            continue
        with conn.transaction():
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table_ident(prev["table"])))
            conn.execute("DELETE FROM public.sync_state WHERE gid = %s", (gid,))
        log.info("Tab gid %s was removed from the sheet; dropped %s.%s", gid, SCHEMA, prev["table"])


def sync_once(cfg: Config, spreadsheet: gspread.Spreadsheet) -> dict:
    tabs = read_all_tabs(spreadsheet)
    stats = {"tabs": len(tabs), "updated": 0, "unchanged": 0, "failed": 0}

    with psycopg.connect(**cfg.db) as conn:
        ensure_schema(conn)
        state = load_state(conn)
        for tab in tabs:
            previous = state.get(tab.gid)
            if previous and previous["hash"] == tab.content_hash and previous["table"] == tab.table:
                stats["unchanged"] += 1
                continue
            try:
                write_tab(conn, tab, previous)
                stats["updated"] += 1
                log.info("Synced tab %-16r -> %s.%s (%d rows)", tab.title, SCHEMA, tab.table, len(tab.rows))
            except psycopg.Error as e:
                stats["failed"] += 1
                log.error("Failed to sync tab %r: %s", tab.title, e)
                record_error(conn, tab.gid, str(e))
        drop_removed_tabs(conn, state, {t.gid for t in tabs})
    return stats


# ---------- main loop ----------

def run(cfg: Config, once: bool) -> int:
    ensure_database(cfg)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    spreadsheet = None
    failures = 0
    while not stop.is_set():
        try:
            if spreadsheet is None:
                spreadsheet = open_sheet(cfg)
                log.info("Connected to sheet %r as %s", spreadsheet.title, cfg.delegated_user)
            stats = sync_once(cfg, spreadsheet)
            log.info(
                "Sync done: %d tabs, %d updated, %d unchanged, %d failed",
                stats["tabs"], stats["updated"], stats["unchanged"], stats["failed"],
            )
            failures = 0
            if once:
                return 1 if stats["failed"] else 0
        except gspread.exceptions.APIError as e:
            failures += 1
            log.error("Google Sheets API error: %s", e)
            spreadsheet = None  # reconnect next cycle (token or permission may have changed)
        except psycopg.OperationalError as e:
            failures += 1
            log.error("Database unavailable: %s", e)
        except Exception:
            failures += 1
            log.exception("Sync cycle failed")
            spreadsheet = None
        if once:
            return 1

        # Back off on repeated failures: 1x, 2x, 4x ... the interval, capped
        wait = cfg.interval_seconds * 2 ** min(failures, 5)
        stop.wait(min(wait, MAX_BACKOFF_SECONDS))
    log.info("Stopped")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror every tab of the PO Google Sheet into PostgreSQL.")
    parser.add_argument("--once", action="store_true", help="sync once and exit")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    sys.exit(run(Config.from_env(), once=args.once))
