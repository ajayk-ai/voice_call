"""PostgreSQL storage for the follow-up: PO data mirrored from the Google Sheet by po_sync (schema `sheets`)
and the follow-up state (schema `followup`).

`PgStore` has the same read_records / append_rows / upsert_rows interface as the Google Sheet
(`sheets.PoSheet`), so the run works unchanged on either.
"""

import json
import os
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from backend.core.env import TIMEZONE, env_int
from backend.po_sync.sync import SCHEMA as SHEETS_SCHEMA
from backend.po_sync.sync import slugify

from backend.po_followup.domain.messages import TAB_HEADERS

FOLLOWUP_SCHEMA = "followup"

# Tabs the run reads from the sheet mirror (schema `sheets`)
SOURCE_TABLES = {"Master": "master", "Phone_Master": "phone_master"}
# Tabs whose state now lives in Postgres: title -> (table, unique key column or None)
STATE_TABLES = {
    "Line_Tracker": ("line_tracker", "Line Key"),
    "Alert_Log": ("alert_log", None),
    "Call_Tasks": ("call_tasks", None),
    "Meeting_Tasks": ("meeting_tasks", None),
}
# Alert_Log is only read for today's dedupe keys; reading two days covers the IST date boundary
ALERT_LOG_READ_DAYS = 2
# Overdue checks kept
KEEP_CHECKS = env_int("FOLLOWUP_KEEP_CHECKS", 120)


def column_name(header: str) -> str:
    return slugify(header, "col")


def db_params() -> dict:
    return {
        "host": os.getenv("PO_DB_HOST", "localhost"),
        "port": int(os.getenv("PO_DB_PORT", "5432")),
        "user": os.getenv("PO_DB_USER", "postgres"),
        "password": os.getenv("PO_DB_PASSWORD", ""),
        "dbname": os.getenv("PO_DB_NAME", "pending_po"),
        # Fail fast instead of hanging a run (and its lock) when the database is unreachable or stuck
        "connect_timeout": 10,
        "options": "-c statement_timeout=300000",
    }


# Advisory lock id for "a run is executing"
RUN_LOCK_ID = 7_310_501


class RunLock:
    """Held for the whole run on its own connection. A PostgreSQL advisory lock, so two server processes
    (or the CLI and the admin page) can never run at the same time and double-send.
    If the process dies, its connection closes and PostgreSQL releases the lock by itself."""

    def __init__(self):
        self.conn = None

    def acquire(self) -> bool:
        conn = psycopg.connect(**db_params(), autocommit=True)
        try:
            got = conn.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_ID,)).fetchone()[0]
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self.conn = conn
        return True

    def release(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()  # ends the session, which releases the lock
            except Exception:
                pass
            self.conn = None


def ensure_database() -> None:
    params = db_params()
    with psycopg.connect(**{**params, "dbname": "postgres"}, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (params["dbname"],)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(params["dbname"])))


def ensure_followup_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(FOLLOWUP_SCHEMA)))
        for title, (table, key) in STATE_TABLES.items():
            cols = [sql.SQL("{} TEXT").format(sql.Identifier(column_name(h))) for h in TAB_HEADERS[title]]
            if key:
                pk = sql.SQL("PRIMARY KEY ({})").format(sql.Identifier(column_name(key)))
            else:
                cols.insert(0, sql.SQL("id BIGSERIAL"))
                pk = sql.SQL("PRIMARY KEY (id)")
            conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({}, {})").format(
                sql.Identifier(FOLLOWUP_SCHEMA, table), sql.SQL(", ").join(cols), pk))
            # columns added in later versions (e.g. 'Last Alert At')
            for h in TAB_HEADERS[title]:
                conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} TEXT").format(
                    sql.Identifier(FOLLOWUP_SCHEMA, table), sql.Identifier(column_name(h))))
        conn.execute("CREATE INDEX IF NOT EXISTS alert_log_date ON followup.alert_log (alert_date)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.settings (
                key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.runs (
                id BIGSERIAL PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ,
                trigger TEXT NOT NULL,       -- manual / auto
                mode TEXT NOT NULL,          -- dry / live
                status TEXT NOT NULL,        -- running / ok / failed
                summary JSONB,
                report JSONB,
                error TEXT)""")
        # One row per overdue check (one per check period), and the lines that were overdue in it
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.overdue_checks (
                period_start TIMESTAMPTZ PRIMARY KEY,
                checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                master_rows INT NOT NULL,
                overdue_lines INT NOT NULL)""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.overdue_snapshot (
                period_start TIMESTAMPTZ NOT NULL REFERENCES followup.overdue_checks ON DELETE CASCADE,
                line_key TEXT NOT NULL,
                vendor_code TEXT NOT NULL,
                od_days NUMERIC,
                PRIMARY KEY (period_start, line_key))""")
        conn.execute("CREATE INDEX IF NOT EXISTS overdue_snapshot_vendor ON followup.overdue_snapshot (vendor_code)")
        # Scenario 4 (management meeting): the vendor's own e-mail ids, kept next to Phone_Master's numbers
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.vendor_contacts (
                vendor_code TEXT PRIMARY KEY,
                emails TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        # One row per Scenario 3 agent call: what was said, and what was understood from it (services/calls.py)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.call_results (
                key TEXT PRIMARY KEY,                 -- ours: travels inside the call script token
                frejun_call_id TEXT,
                vendor_code TEXT NOT NULL,
                vendor_name TEXT NOT NULL DEFAULT '',
                mobile TEXT NOT NULL DEFAULT '',
                run_trigger TEXT NOT NULL DEFAULT '',
                lines JSONB NOT NULL DEFAULT '[]',    -- the overdue lines the agent was given
                status TEXT NOT NULL DEFAULT 'placed',  -- placed / in_call / completed / no_response / failed
                placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                connected_at TIMESTAMPTZ,
                ended_at TIMESTAMPTZ,
                duration_seconds INT,
                transcript JSONB NOT NULL DEFAULT '[]',
                recording_url TEXT,
                events JSONB NOT NULL DEFAULT '[]',   -- FreJun status callbacks for this call
                analysis JSONB,
                analysis_status TEXT NOT NULL DEFAULT 'pending',  -- pending / ok / failed / skipped
                analysis_error TEXT)""")
        # Dashboard users, created on the admin page (services/users.py). Passwords are stored as salted scrypt hashes.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.users (
                username TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                failed_logins INT NOT NULL DEFAULT 0,
                locked_until TIMESTAMPTZ,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_login_at TIMESTAMPTZ)""")
        # Who did what on the dashboard (start / stop / run / vendor changes / logins)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS followup.audit_log (
                id BIGSERIAL PRIMARY KEY,
                at TIMESTAMPTZ NOT NULL DEFAULT now(),
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail JSONB NOT NULL DEFAULT '{}')""")
        conn.execute("CREATE INDEX IF NOT EXISTS audit_log_at ON followup.audit_log (at DESC)")
        # Tidy databases made by earlier versions
        conn.execute("ALTER TABLE followup.users DROP COLUMN IF EXISTS role")
        conn.execute("ALTER TABLE followup.audit_log DROP COLUMN IF EXISTS env")
        conn.execute("""UPDATE followup.settings SET key = 'vendor_codes' WHERE key = 'pilot_vendor_codes'
                        AND NOT EXISTS (SELECT 1 FROM followup.settings WHERE key = 'vendor_codes')""")
        conn.execute("CREATE INDEX IF NOT EXISTS call_results_vendor ON followup.call_results (vendor_code, placed_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS call_results_frejun ON followup.call_results (frejun_call_id)")


@contextmanager
def connect():
    with psycopg.connect(**db_params(), row_factory=dict_row) as conn:
        yield conn


def table_exists(conn, schema: str, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{table}",)).fetchone()["ok"]


def mirrored_columns(conn, table: str) -> list[tuple[str, str]]:
    """[(column, original sheet header)] for a po_sync table; the header is stored as the column comment."""
    rows = conn.execute(
        """SELECT a.attname AS col, col_description(a.attrelid, a.attnum) AS header
           FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
           ORDER BY a.attnum""",
        (f"{SHEETS_SCHEMA}.{table}",),
    ).fetchall()
    return [(r["col"], r["header"] or r["col"]) for r in rows if r["col"] != "sheet_row"]


class PgStore:
    def read_records(self, title: str) -> list[dict]:
        with connect() as conn:
            if title in SOURCE_TABLES:
                table = SOURCE_TABLES[title]
                if not table_exists(conn, SHEETS_SCHEMA, table):
                    raise RuntimeError(f"Database has no {SHEETS_SCHEMA}.{table}; run the sheet sync")
                cols = mirrored_columns(conn, table)
                rows = conn.execute(sql.SQL("SELECT {} FROM {} ORDER BY sheet_row").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c, _ in cols),
                    sql.Identifier(SHEETS_SCHEMA, table))).fetchall()
                return [{h: (r[c] or "") for c, h in cols} for r in rows]

            table, _ = STATE_TABLES[title]
            headers = TAB_HEADERS[title]
            query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(FOLLOWUP_SCHEMA, table))
            if title == "Alert_Log":
                query = sql.SQL("SELECT * FROM followup.alert_log WHERE alert_date >= to_char("
                                "(now() AT TIME ZONE %s) - make_interval(days => %s), 'YYYY-MM-DD')")
                rows = conn.execute(query, (TIMEZONE.key, ALERT_LOG_READ_DAYS)).fetchall()
            else:
                rows = conn.execute(query).fetchall()
            return [{h: (r.get(column_name(h)) or "") for h in headers} for r in rows]

    def append_rows(self, title: str, rows: list[dict]) -> None:
        if not rows:
            return
        table, _ = STATE_TABLES[title]
        headers = TAB_HEADERS[title]
        cols = [column_name(h) for h in headers]
        stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(FOLLOWUP_SCHEMA, table),
            sql.SQL(", ").join(sql.Identifier(c) for c in cols),
            sql.SQL(", ").join(sql.Placeholder() * len(cols)))
        with connect() as conn, conn.cursor() as cur:
            cur.executemany(stmt, [[_text(r.get(h)) for h in headers] for r in rows])

    def upsert_rows(self, title: str, rows: list[dict], key: str) -> None:
        """Insert, or update only the columns present in each row (like the sheet's appendOrUpdate)."""
        if not rows:
            return
        table, table_key = STATE_TABLES[title]
        if key != table_key:
            raise ValueError(f"{title} is keyed on {table_key!r}, not {key!r}")
        known = set(TAB_HEADERS[title])
        with connect() as conn, conn.cursor() as cur:
            for r in rows:
                headers = [h for h in r if h in known]
                cols = [column_name(h) for h in headers]
                updates = [c for c in cols if c != column_name(key)]
                stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO {}").format(
                    sql.Identifier(FOLLOWUP_SCHEMA, table),
                    sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                    sql.SQL(", ").join(sql.Placeholder() * len(cols)),
                    sql.Identifier(column_name(key)),
                    sql.SQL("UPDATE SET {}").format(sql.SQL(", ").join(
                        sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in updates))
                    if updates else sql.SQL("NOTHING"))
                cur.execute(stmt, [_text(r[h]) for h in headers])


    # ----- daily overdue checks (domain/overdue.py) -----

    def vendor_emails(self) -> dict[str, str]:
        return vendor_emails()

    def record_check(self, period_start, lines: dict[str, dict], master_rows: int) -> None:
        """Store which lines are overdue in this check period. Running again in the same period replaces it,
        so the latest check of the day wins. Old checks beyond KEEP_CHECKS are dropped."""
        with connect() as conn, conn.transaction():
            conn.execute("DELETE FROM followup.overdue_checks WHERE period_start = %s", (period_start,))
            conn.execute("INSERT INTO followup.overdue_checks (period_start, master_rows, overdue_lines) VALUES (%s, %s, %s)",
                         (period_start, master_rows, len(lines)))
            with conn.cursor() as cur, cur.copy(
                    "COPY followup.overdue_snapshot (period_start, line_key, vendor_code, od_days) FROM STDIN") as copy:
                for key, line in lines.items():
                    copy.write_row((period_start, key, line["vendor_code"], line["od"]))
            conn.execute("DELETE FROM followup.overdue_checks WHERE period_start NOT IN (SELECT period_start FROM "
                         "followup.overdue_checks ORDER BY period_start DESC LIMIT %s)", (KEEP_CHECKS,))

    def recent_checks(self, upto, limit: int = 7) -> list[tuple]:
        """[(period start, {overdue line keys})] of the last `limit` checks at or before `upto`, newest first."""
        with connect() as conn:
            starts = [r["period_start"] for r in conn.execute(
                "SELECT period_start FROM followup.overdue_checks WHERE period_start <= %s "
                "ORDER BY period_start DESC LIMIT %s", (upto, limit)).fetchall()]
            keys: dict = {p: set() for p in starts}
            if starts:
                for r in conn.execute("SELECT period_start, line_key FROM followup.overdue_snapshot "
                                      "WHERE period_start = ANY(%s)", (starts,)):
                    keys[r["period_start"]].add(r["line_key"])
        return [(p, keys[p]) for p in starts]


def _text(v):
    return None if v is None or v == "" else str(v)


# ---------- mirrored tabs edited from the dashboard (Phone_Master) ----------

def source_headers(title: str) -> list[str]:
    """Original sheet headers of a mirrored tab (Master / Phone_Master), in sheet order; [] if not there yet."""
    table = SOURCE_TABLES[title]
    with connect() as conn:
        if not table_exists(conn, SHEETS_SCHEMA, table):
            return []
        return [h for _, h in mirrored_columns(conn, table)]


def upsert_source_rows(title: str, rows: list[dict], key: str) -> None:
    """Write rows of a mirrored tab, matched on their `key` header: update the given columns, or add the row
    at the end. All or nothing. The Google Sheet is the source: write it there first; the next
    sheet sync replaces this copy with what the sheet holds."""
    table = SOURCE_TABLES[title]
    with connect() as conn, conn.transaction():
        if not table_exists(conn, SHEETS_SCHEMA, table):
            raise ValueError(f"The database has no {title} yet; run the sheet sync")
        cols: dict[str, str] = {}
        for c, h in mirrored_columns(conn, table):
            cols.setdefault(h, c)
        unknown = {h for r in rows for h in r} - set(cols)
        if unknown:
            raise ValueError(f"{title} has no column(s) {sorted(unknown)}")
        ident = sql.Identifier(SHEETS_SCHEMA, table)
        for row in rows:
            names = [cols[h] for h in row]
            values = [_text(v) for v in row.values()]
            updated = conn.execute(sql.SQL("UPDATE {} SET {} WHERE trim({}) = %s").format(
                ident, sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in names),
                sql.Identifier(cols[key])), [*values, str(row[key]).strip()]).rowcount
            if not updated:
                conn.execute(sql.SQL("INSERT INTO {} (sheet_row, {}) VALUES ((SELECT coalesce(max(sheet_row), 1) + 1 FROM {}), {})").format(
                    ident, sql.SQL(", ").join(sql.Identifier(c) for c in names), ident,
                    sql.SQL(", ").join(sql.Placeholder() * len(names))), values)


# ---------- environment maintenance ----------

def init() -> None:
    """Create the database and the follow-up tables; run once at startup."""
    ensure_database()
    with connect() as conn:
        ensure_followup_schema(conn)


def master_synced_at() -> str | None:
    """When the PO Master was last copied from the Google Sheet."""
    with connect() as conn:
        if not table_exists(conn, "public", "sync_state"):
            return None
        r = conn.execute("SELECT last_synced_at FROM public.sync_state WHERE table_name = 'master'").fetchone()
    return r["last_synced_at"].isoformat() if r else None


def table_rows(title: str, limit: int = 200) -> list[dict]:
    table, key = STATE_TABLES[title]
    order = "id DESC" if not key else "updated_at DESC NULLS LAST"
    with connect() as conn:
        rows = conn.execute(sql.SQL("SELECT * FROM {} ORDER BY " + order + " LIMIT %s").format(
            sql.Identifier(FOLLOWUP_SCHEMA, table)), (limit,)).fetchall()
    return [{h: (r.get(column_name(h)) or "") for h in TAB_HEADERS[title]} for r in rows]


# How each follow-up table refers to a vendor: (header, LIKE pattern for a vendor code)
_VENDOR_MATCH = {
    "Line_Tracker": ("Vendor Code", "{}"),
    "Alert_Log": ("Vendor", "%({})"),  # "Vendor Name (400186)"
    "Call_Tasks": ("Task ID", "{}-CALL-%"),
    "Meeting_Tasks": ("Meeting ID", "{}-MEET-%"),
}


def _like_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def vendor_rows(title: str, vendor_code: str, limit: int = 500) -> list[dict]:
    """One vendor's rows of a follow-up table, oldest first."""
    table, key = STATE_TABLES[title]
    header, pattern = _VENDOR_MATCH[title]
    order = sql.SQL("id") if not key else sql.Identifier(column_name(key))
    with connect() as conn:
        rows = conn.execute(sql.SQL("SELECT * FROM {} WHERE {} LIKE %s ORDER BY {} LIMIT %s").format(
            sql.Identifier(FOLLOWUP_SCHEMA, table), sql.Identifier(column_name(header)), order),
            (pattern.format(_like_literal(vendor_code)), limit)).fetchall()
    return [{h: (r.get(column_name(h)) or "") for h in TAB_HEADERS[title]} for r in rows]


def get_settings() -> dict:
    with connect() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM followup.settings").fetchall()}


def save_settings(values: dict) -> None:
    with connect() as conn, conn.transaction():
        for k, v in values.items():
            conn.execute(
                "INSERT INTO followup.settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (k, json.dumps(v)))


# ---------- vendor e-mail ids (Scenario 4) ----------

def vendor_emails() -> dict[str, str]:
    """Vendor code -> comma-separated e-mail ids saved on the dashboard."""
    with connect() as conn:
        return {r["vendor_code"]: r["emails"] for r in
                conn.execute("SELECT vendor_code, emails FROM followup.vendor_contacts WHERE emails <> ''").fetchall()}


def save_vendor_emails(emails: dict[str, str]) -> None:
    """Set (or, with a blank value, remove) several vendors' e-mail ids in one transaction."""
    with connect() as conn, conn.transaction():
        for code, value in emails.items():
            if value:
                conn.execute(
                    "INSERT INTO followup.vendor_contacts (vendor_code, emails) VALUES (%s, %s) "
                    "ON CONFLICT (vendor_code) DO UPDATE SET emails = EXCLUDED.emails, updated_at = now()", (code, value))
            else:
                conn.execute("DELETE FROM followup.vendor_contacts WHERE vendor_code = %s", (code,))
