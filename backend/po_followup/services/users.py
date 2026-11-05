"""Dashboard users: created by the admin, signed in with a user id and password.

Users and the audit log live in followup.users and followup.audit_log. Passwords are salted scrypt hashes. A sign-in gives a signed session token (HMAC) that the dashboard
sends in the X-Session header. The admin is not a user row: see admin_login. Everything tunable is in .env: ADMIN_USERNAME, SESSION_SECRET, SESSION_HOURS, PASSWORD_MIN_LENGTH,
LOGIN_MAX_FAILURES, LOGIN_LOCK_MINUTES.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from backend.core.env import env_int
from backend.po_followup.db import store

ADMIN, SCM = "admin", "scm"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
_scrypt = dict(n=2 ** 14, r=8, p=1, dklen=32)
_process_secret = secrets.token_bytes(32)  # used when SESSION_SECRET is not set: sessions end at a restart


class AuthError(Exception):
    pass


def admin_password() -> str:
    return os.getenv("APP_PASSWORD", "")


def admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "admin").strip().lower() or "admin"


def session_hours() -> int:
    return max(1, env_int("SESSION_HOURS", 12))


def min_password_length() -> int:
    return max(6, env_int("PASSWORD_MIN_LENGTH", 8))


def _secret() -> bytes:
    configured = os.getenv("SESSION_SECRET", "").strip()
    if configured:
        return configured.encode()
    admin = admin_password()  # stable across restarts
    return hashlib.sha256(b"session:" + admin.encode()).digest() if admin else _process_secret


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_scrypt)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_scrypt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, bytes.fromhex(digest))


def check_new_password(password: str) -> None:
    need = min_password_length()
    if len(password) < need:
        raise ValueError(f"The password needs at least {need} characters.")
    if password.isdigit() or password.isalpha():
        raise ValueError("Use letters and numbers together in the password.")


def clean_username(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise ValueError("User id: 3-40 characters, lower-case letters, numbers, . _ - (start with a letter or number).")
    return name


# ---------- session tokens ----------

def make_token(username: str, role: str = SCM) -> str:
    body = base64.urlsafe_b64encode(json.dumps(
        {"u": username, "r": role, "exp": int(time.time()) + session_hours() * 3600}).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str | None) -> dict | None:
    """The user a valid, unexpired token belongs to ({username, role}), else None."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest(), sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return {"username": data["u"], "role": data["r"]}


def current(token: str | None) -> dict | None:
    """Like read_token, but the user must still exist and be active (disabling a user ends their session)."""
    t = read_token(token)
    if not t:
        return None
    if t["role"] == ADMIN:
        return {"username": t["username"], "role": ADMIN, "display_name": "Admin"}
    with store.connect() as conn:
        row = conn.execute("SELECT username, display_name, active FROM followup.users WHERE username = %s",
                           (t["username"],)).fetchone()
    if not row or not row["active"]:
        return None
    return {"username": row["username"], "role": SCM, "display_name": row["display_name"]}


# ---------- sign in ----------

def _signed_in(username: str, role: str, display_name: str) -> dict:
    return {"token": make_token(username, role), "username": username, "role": role, "display_name": display_name}


def login(username: str, password: str) -> dict:
    name = (username or "").strip().lower()
    max_fail, lock_min = max(1, env_int("LOGIN_MAX_FAILURES", 5)), max(1, env_int("LOGIN_LOCK_MINUTES", 15))
    with store.connect() as conn, conn.transaction():
        row = conn.execute("SELECT * FROM followup.users WHERE username = %s FOR UPDATE", (name,)).fetchone()
        if not row:
            hash_password(password)  # same time as a real check, so unknown ids are not revealed by speed
            raise AuthError("Wrong user id or password.")
        locked = conn.execute("SELECT %s > now() AS l", (row["locked_until"],)).fetchone()["l"] if row["locked_until"] else False
        if locked:
            raise AuthError(f"Too many wrong attempts. Try again in {lock_min} minutes or ask the admin.")
        if not verify_password(password, row["password_hash"]):
            fails = row["failed_logins"] + 1
            if fails >= max_fail:
                conn.execute("UPDATE followup.users SET failed_logins = 0, locked_until = now() + make_interval(mins => %s) "
                             "WHERE username = %s", (lock_min, name))
            else:
                conn.execute("UPDATE followup.users SET failed_logins = %s WHERE username = %s", (fails, name))
            raise AuthError("Wrong user id or password.")
        if not row["active"]:
            raise AuthError("This account is switched off. Ask the admin.")
        conn.execute("UPDATE followup.users SET failed_logins = 0, locked_until = NULL, last_login_at = now() "
                     "WHERE username = %s", (name,))
    audit(name, "login")
    return _signed_in(name, SCM, row["display_name"])


_admin_failures = {"count": 0, "locked_until": 0.0}


def admin_login(username: str, password: str) -> dict:
    """The admin signs in with ADMIN_USERNAME and APP_PASSWORD from .env; not a row in followup.users."""
    expected = admin_password()
    if not expected:
        raise AuthError("Set APP_PASSWORD on the server first.")
    if _admin_failures["locked_until"] > time.time():
        raise AuthError("Too many wrong attempts. Try again later.")
    name_ok = hmac.compare_digest((username or "").strip().lower().encode(), admin_username().encode())
    if not (hmac.compare_digest((password or "").encode(), expected.encode()) and name_ok):
        _admin_failures["count"] += 1
        if _admin_failures["count"] >= max(1, env_int("LOGIN_MAX_FAILURES", 5)):
            _admin_failures.update(count=0, locked_until=time.time() + max(1, env_int("LOGIN_LOCK_MINUTES", 15)) * 60)
        raise AuthError("Wrong user id or password.")
    _admin_failures.update(count=0, locked_until=0.0)
    audit(admin_username(), "login")
    return _signed_in(admin_username(), ADMIN, "Admin")


# ---------- admin: manage users ----------

def _public(row: dict) -> dict:
    return {"username": row["username"], "display_name": row["display_name"],
            "active": row["active"], "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "last_login_at": row["last_login_at"].isoformat() if row["last_login_at"] else None,
            "locked": bool(row.get("locked_until")) and row["locked_until"].timestamp() > time.time()}


def list_users() -> list[dict]:
    with store.connect() as conn:
        return [_public(r) for r in conn.execute("SELECT * FROM followup.users ORDER BY username").fetchall()]


def create_user(username: str, password: str, display_name: str = "", by: str = ADMIN) -> dict:
    name = clean_username(username)
    check_new_password(password)
    with store.connect() as conn, conn.transaction():
        if conn.execute("SELECT 1 FROM followup.users WHERE username = %s", (name,)).fetchone():
            raise ValueError(f"User id {name} already exists.")
        row = conn.execute(
            "INSERT INTO followup.users (username, display_name, password_hash, created_by) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (name, display_name.strip()[:100] or name, hash_password(password), by)).fetchone()
    audit(by, "user_created", detail={"username": name})
    return _public(row)


def update_user(username: str, by: str = ADMIN, password: str | None = None, active: bool | None = None,
                display_name: str | None = None) -> dict:
    sets, args = [], []
    if password is not None:
        check_new_password(password)
        sets += ["password_hash = %s", "failed_logins = 0", "locked_until = NULL"]
        args.append(hash_password(password))
    if active is not None:
        sets.append("active = %s")
        args.append(active)
    if display_name is not None:
        sets.append("display_name = %s")
        args.append(display_name.strip()[:100])
    if not sets:
        raise ValueError("Nothing to change.")
    with store.connect() as conn, conn.transaction():
        row = conn.execute(f"UPDATE followup.users SET {', '.join(sets)} WHERE username = %s RETURNING *",
                           (*args, username)).fetchone()
    if not row:
        raise ValueError(f"No user {username}.")
    audit(by, "user_updated", detail={"username": username, "password_reset": password is not None, "active": active})
    return _public(row)


def delete_user(username: str, by: str = ADMIN) -> None:
    with store.connect() as conn, conn.transaction():
        n = conn.execute("DELETE FROM followup.users WHERE username = %s", (username,)).rowcount
    if not n:
        raise ValueError(f"No user {username}.")
    audit(by, "user_deleted", detail={"username": username})


# ---------- audit log ----------

def audit(actor: str, action: str, detail: dict | None = None) -> None:
    """Best effort: a failing audit write never blocks the action."""
    try:
        with store.connect() as conn:
            conn.execute("INSERT INTO followup.audit_log (actor, action, detail) VALUES (%s, %s, %s)",
                         (actor, action, json.dumps(detail or {})))
    except Exception:
        pass


def recent_audit(limit: int = 100) -> list[dict]:
    with store.connect() as conn:
        rows = conn.execute("SELECT at, actor, action, detail FROM followup.audit_log ORDER BY at DESC LIMIT %s",
                            (limit,)).fetchall()
    return [{**r, "at": r["at"].isoformat()} for r in rows]
