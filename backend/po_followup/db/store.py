"""PostgreSQL storage for the follow-up workflow, one database per environment.

    production: PO data mirrored from the Google Sheet by po_sync (schema `sheets`)
                + follow-up state in schema `followup`
    test:       a copy of production's Master / Phone_Master you can edit freely
                + its own follow-up state, so test runs never touch production

`PgStore` has the same read_records / append_rows / upsert_rows interface as the Google Sheet
(`sheets.PoSheet`), so the daily run works unchanged on either.
"""

import json
import os
import re
import zlib
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from backend.po_sync.sync import SCHEMA as SHEETS_SCHEMA
from backend.po_sync.sync import Tab, ensure_schema, load_state, slugify, write_tab

from backend.po_followup.domain.messages import TAB_HEADERS

ENVIRONMENTS = ("prod", "test")
FOLLOWUP_SCHEMA = "followup"

# Tabs the run reads from the sheet mirror (schema `sheets`) and copies into test
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


def column_name(header: str) -> str:
    return slugify(header, "col")


def db_name(env: str) -> str:
    if env not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment {env!r} (use prod or test)")
    if env == "prod":
        return os.getenv("FOLLOWUP_PROD_DB") or os.getenv("PO_DB_NAME", "pending_po")
    return os.getenv("FOLLOWUP_TEST_DB", "pending_po_test")


def db_params(env: str) -> dict:
    return {
        "host": os.getenv("PO_DB_HOST", "localhost"),
        "port": int(os.getenv("PO_DB_PORT", "5432")),
        "user": os.getenv("PO_DB_USER", "postgres"),
        "password": os.getenv("PO_DB_PASSWORD", ""),
        "dbname": db_name(env),
        # Fail fast instead of hanging a run (and its lock) when the database is unreachable or stuck
        "connect_timeout": 10,
        "options": "-c statement_timeout=300000",
    }


# Advisory lock id for "a run is executing"; each environment has its own database, so one id is enough
RUN_LOCK_ID = 7_310_501


class RunLock:
    """Held for the whole run on its own connection. A PostgreSQL advisory lock, so two server processes
    (or the CLI and the admin page) can never run the same environment at the same time and double-send.
    If the process dies, its connection closes and PostgreSQL releases the lock by itself."""

    def __init__(self, env: str):
        self.env = env
        self.conn = None

    def acquire(self) -> bool:
        conn = psycopg.connect(**db_params(self.env), autocommit=True)
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


def ensure_database(env: str) -> None:
    params = db_params(env)
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
                mode TEXT NOT NULL,          -- preview / dry / live
                status TEXT NOT NULL,        -- running / ok / failed
                summary JSONB,
                report JSONB,
                error TEXT)""")


@contextmanager
def connect(env: str):
    with psycopg.connect(**db_params(env), row_factory=dict_row) as conn:
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
    def __init__(self, env: str):
        self.env = env

    def read_records(self, title: str) -> list[dict]:
        with connect(self.env) as conn:
            if title in SOURCE_TABLES:
                table = SOURCE_TABLES[title]
                if not table_exists(conn, SHEETS_SCHEMA, table):
                    raise RuntimeError(f"{self.env} database has no {SHEETS_SCHEMA}.{table} "
                                       f"({'run the sheet sync' if self.env == 'prod' else 'copy production data into test'})")
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
                                "(now() AT TIME ZONE 'Asia/Kolkata') - make_interval(days => %s), 'YYYY-MM-DD')")
                rows = conn.execute(query, (ALERT_LOG_READ_DAYS,)).fetchall()
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
        with connect(self.env) as conn, conn.cursor() as cur:
            cur.executemany(stmt, [[_text(r.get(h)) for h in headers] for r in rows])

    def upsert_rows(self, title: str, rows: list[dict], key: str) -> None:
        """Insert, or update only the columns present in each row (like the sheet's appendOrUpdate)."""
        if not rows:
            return
        table, table_key = STATE_TABLES[title]
        if key != table_key:
            raise ValueError(f"{title} is keyed on {table_key!r}, not {key!r}")
        known = set(TAB_HEADERS[title])
        with connect(self.env) as conn, conn.cursor() as cur:
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


def _text(v):
    return None if v is None or v == "" else str(v)


# ---------- environment maintenance ----------

def init_environment(env: str) -> None:
    ensure_database(env)
    with connect(env) as conn:
        ensure_followup_schema(conn)


def copy_prod_to_test() -> dict:
    """Copy production Master + Phone_Master into the test database and clear test follow-up state."""
    copied = {}
    with psycopg.connect(**db_params("prod"), row_factory=dict_row) as src, psycopg.connect(**db_params("test")) as dst:
        ensure_schema(dst)
        state = load_state(dst)
        for title, table in SOURCE_TABLES.items():
            if not table_exists(src, SHEETS_SCHEMA, table):
                raise RuntimeError(f"Production has no {SHEETS_SCHEMA}.{table}; sync the sheet first")
            cols = mirrored_columns(src, table)
            rows = src.execute(sql.SQL("SELECT sheet_row, {} FROM {} ORDER BY sheet_row").format(
                sql.SQL(", ").join(sql.Identifier(c) for c, _ in cols),
                sql.Identifier(SHEETS_SCHEMA, table))).fetchall()
            gid_row = src.execute("SELECT gid FROM public.sync_state WHERE table_name = %s", (table,)).fetchone()
            tab = Tab(
                gid=gid_row["gid"] if gid_row else zlib.crc32(table.encode()), title=title, table=table,
                headers=[h for _, h in cols], columns=[c for c, _ in cols],
                rows=[(r["sheet_row"], [r[c] for c, _ in cols]) for r in rows],
                content_hash=f"copied-from-prod-{len(rows)}",
            )
            write_tab(dst, tab, state.get(tab.gid))
            copied[title] = len(rows)
    clear_state("test")
    if os.getenv("FOLLOWUP_TEST_SCENARIO_MOBILE"):
        copied["scenario_numbers_set"] = set_test_scenario_numbers(os.environ["FOLLOWUP_TEST_SCENARIO_MOBILE"])
    return copied


def set_test_scenario_numbers(mobile: str) -> int:
    """Point every test vendor's Phone_Master Scenario 1/2/3 numbers at `mobile`, so test runs only ever
    message that one number. Test only. Returns the number of vendor rows updated."""
    from backend.po_followup.domain.escalation import norm

    with connect("test") as conn:
        if not table_exists(conn, SHEETS_SCHEMA, "phone_master"):
            raise RuntimeError("Test has no Phone_Master; copy production to test first")
        cols = [c for c, h in mirrored_columns(conn, "phone_master")
                if any(re.fullmatch(rf"scenario {n}( .*)?", norm(h)) for n in (1, 2, 3))]
        if not cols:
            raise RuntimeError("Test Phone_Master has no Scenario 1/2/3 columns")
        return conn.execute(sql.SQL("UPDATE {} SET {}").format(
            sql.Identifier(SHEETS_SCHEMA, "phone_master"),
            sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in cols)),
            [mobile] * len(cols)).rowcount


def clear_state(env: str) -> None:
    """Empty the follow-up tables (tracker, alert log, tasks, run history). Test only."""
    if env != "test":
        raise ValueError("Only the test environment can be cleared")
    with connect(env) as conn:
        ensure_followup_schema(conn)
        conn.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(
            [sql.Identifier(FOLLOWUP_SCHEMA, t) for t, _ in STATE_TABLES.values()]
            + [sql.Identifier(FOLLOWUP_SCHEMA, "runs")])))


def import_sheet_state(env: str = "prod") -> dict:
    """One-time: copy Line_Tracker / Alert_Log / Call_Tasks / Meeting_Tasks rows that n8n wrote in the
    Google Sheet (mirrored by po_sync) into the follow-up tables, so escalation continues where it left off."""
    store = PgStore(env)
    done = {}
    with connect(env) as conn:
        ensure_followup_schema(conn)
        if conn.execute("SELECT count(*) AS n FROM followup.line_tracker").fetchone()["n"]:
            raise RuntimeError("followup.line_tracker already has rows; import only runs on an empty tracker")
        for title, (table, key) in STATE_TABLES.items():
            if not table_exists(conn, SHEETS_SCHEMA, table):
                continue
            cols = mirrored_columns(conn, table)
            rows = conn.execute(sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c, _ in cols),
                sql.Identifier(SHEETS_SCHEMA, table))).fetchall()
            done[title] = [{h: (r[c] or "") for c, h in cols} for r in rows]
    for title, rows in done.items():
        key = STATE_TABLES[title][1]
        if key:
            store.upsert_rows(title, [r for r in rows if r.get(key)], key)
        else:
            store.append_rows(title, rows)
    return {t: len(r) for t, r in done.items()}


def stats(env: str) -> dict:
    """Numbers for the admin page."""
    out: dict = {"database": db_name(env)}
    with connect(env) as conn:
        ensure_followup_schema(conn)
        if table_exists(conn, SHEETS_SCHEMA, "master"):
            cols = dict((h.strip().lower(), c) for c, h in mirrored_columns(conn, "master"))
            od, vendor = cols.get("od days"), cols.get("vendor code")
            out["master_rows"] = conn.execute("SELECT count(*) AS n FROM sheets.master").fetchone()["n"]
            if od and vendor:
                r = conn.execute(sql.SQL(
                    "SELECT count(*) AS lines, count(DISTINCT {v}) AS vendors FROM sheets.master "
                    "WHERE {o} ~ '^-?[0-9.,]+$' AND replace({o}, ',', '')::numeric > 0").format(
                    o=sql.Identifier(od), v=sql.Identifier(vendor))).fetchone()
                out["pending_lines"], out["pending_vendors"] = r["lines"], r["vendors"]
        else:
            out["master_rows"] = None
        if table_exists(conn, "public", "sync_state"):
            r = conn.execute("SELECT last_synced_at FROM public.sync_state WHERE table_name = 'master'").fetchone()
            out["master_synced_at"] = r["last_synced_at"].isoformat() if r else None
        out["tracker"] = conn.execute(
            "SELECT coalesce(status, '-') AS status, coalesce(escalation_level, '-') AS level, count(*) AS n "
            "FROM followup.line_tracker GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
        out["alert_log_rows"] = conn.execute("SELECT count(*) AS n FROM followup.alert_log").fetchone()["n"]
    return out


def table_rows(env: str, title: str, limit: int = 200) -> list[dict]:
    table, key = STATE_TABLES[title]
    order = "id DESC" if not key else "updated_at DESC NULLS LAST"
    with connect(env) as conn:
        ensure_followup_schema(conn)
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


def vendor_rows(env: str, title: str, vendor_code: str, limit: int = 500) -> list[dict]:
    """One vendor's rows of a follow-up table, oldest first."""
    table, key = STATE_TABLES[title]
    header, pattern = _VENDOR_MATCH[title]
    order = sql.SQL("id") if not key else sql.Identifier(column_name(key))
    with connect(env) as conn:
        ensure_followup_schema(conn)
        rows = conn.execute(sql.SQL("SELECT * FROM {} WHERE {} LIKE %s ORDER BY {} LIMIT %s").format(
            sql.Identifier(FOLLOWUP_SCHEMA, table), sql.Identifier(column_name(header)), order),
            (pattern.format(_like_literal(vendor_code)), limit)).fetchall()
    return [{h: (r.get(column_name(h)) or "") for h in TAB_HEADERS[title]} for r in rows]


def alert_days(env: str, statuses: set[str]) -> list[dict]:
    """Alert_Log condensed to one row per vendor, level and day: the first time a message with one of
    `statuses` went out, and whether every one of them was simulated (DRY_RUN)."""
    with connect(env) as conn:
        ensure_followup_schema(conn)
        return conn.execute(
            "SELECT vendor, escalation_level AS level, alert_date, min(timestamp) AS first_at, "
            "bool_and(status = 'DRY_RUN') AS simulated FROM followup.alert_log "
            "WHERE status = ANY(%s) GROUP BY 1, 2, 3 ORDER BY 3, 4", (sorted(statuses),)).fetchall()


def reset_vendor(env: str, vendor_code: str) -> dict:
    """Forget one vendor's escalation (tracker, alert log, tasks) so it starts again at Scenario 1. Test only."""
    if env != "test":
        raise ValueError("Only the test environment can be reset")
    deleted = {}
    with connect(env) as conn, conn.transaction():
        ensure_followup_schema(conn)
        for title, (header, pattern) in _VENDOR_MATCH.items():
            table, _ = STATE_TABLES[title]
            deleted[title] = conn.execute(sql.SQL("DELETE FROM {} WHERE {} LIKE %s").format(
                sql.Identifier(FOLLOWUP_SCHEMA, table), sql.Identifier(column_name(header))),
                (pattern.format(_like_literal(vendor_code)),)).rowcount
    return deleted


def get_settings(env: str) -> dict:
    with connect(env) as conn:
        ensure_followup_schema(conn)
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM followup.settings").fetchall()}


def save_settings(env: str, values: dict) -> None:
    with connect(env) as conn, conn.transaction():
        for k, v in values.items():
            conn.execute(
                "INSERT INTO followup.settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (k, json.dumps(v)))
