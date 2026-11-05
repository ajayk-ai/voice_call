#!/usr/bin/env bash
# Create the PostgreSQL app role and databases on this server, and write the credentials to .env.
#
#     sudo -v && bash scripts/setup_db.sh
#
# Safe to re-run: an existing role keeps its password unless you pass --reset-password.
set -euo pipefail

ROLE="${PO_DB_USER:-po_app}"
DBS=("${PO_DB_NAME:-pending_po}")
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
RESET=false
[[ "${1:-}" == "--reset-password" ]] && RESET=true

psql_admin() { sudo -u postgres psql -v ON_ERROR_STOP=1 -qAt "$@"; }

role_exists=$(psql_admin -c "SELECT 1 FROM pg_roles WHERE rolname = '$ROLE'")

if [[ -z "$role_exists" || "$RESET" == true ]]; then
    PW=$(openssl rand -base64 24 | tr -d '/+=')
    # CREATEDB: the app creates its own databases (backend/po_sync, backend/po_followup). No superuser.
    # Password goes through a psql variable so it never appears in the process list.
    if [[ -z "$role_exists" ]]; then
        printf '%s' "CREATE ROLE \"$ROLE\" LOGIN CREATEDB PASSWORD '$PW';" | psql_admin
    else
        printf '%s' "ALTER ROLE \"$ROLE\" PASSWORD '$PW';" | psql_admin
    fi
    NEW_PW=true
else
    NEW_PW=false
    echo "Role $ROLE already exists; password unchanged (use --reset-password to rotate)."
fi

for db in "${DBS[@]}"; do
    if [[ -z "$(psql_admin -c "SELECT 1 FROM pg_database WHERE datname = '$db'")" ]]; then
        sudo -u postgres createdb -O "$ROLE" "$db"
        echo "Created database $db"
    fi
done

if [[ "$NEW_PW" == true ]]; then
    touch "$ENV_FILE" && chmod 600 "$ENV_FILE"
    [[ -s "$ENV_FILE" ]] || cp "$(dirname "$ENV_FILE")/.env.example" "$ENV_FILE"
    set_kv() {  # replace KEY=... if present, else append
        if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
    }
    set_kv PO_DB_HOST localhost
    set_kv PO_DB_PORT 5432
    set_kv PO_DB_USER "$ROLE"
    set_kv PO_DB_PASSWORD "$PW"
    echo "Wrote PO_DB_* to $ENV_FILE (mode 600)."
fi

# Verify the login the app will use
get_kv() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2-; }  # not `source`: .env values may contain spaces
PGPASSWORD="$(get_kv PO_DB_PASSWORD)" psql -h "$(get_kv PO_DB_HOST)" -p "$(get_kv PO_DB_PORT)" -U "$(get_kv PO_DB_USER)" -d "${DBS[0]}" -Atc "SELECT 'login OK as ' || current_user"
