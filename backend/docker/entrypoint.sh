#!/bin/sh
# Phase 8 container entrypoint. Seeds demo data on first run only (if the
# SQLite file at AFRA_DB_PATH doesn't exist yet - e.g. a fresh named
# volume), then starts the API. Re-running `docker compose up` against an
# existing volume never re-seeds or wipes data; see the README's "reset /
# reseed" instructions for how to force a fresh seed deliberately.
set -e

DB_PATH="${AFRA_DB_PATH:-/data/afra_demo.db}"

if [ ! -f "$DB_PATH" ]; then
    echo "[entrypoint] no database found at $DB_PATH - seeding demo data..."
    python scripts/seed_demo_data.py
else
    echo "[entrypoint] using existing database at $DB_PATH"
fi

exec python scripts/run_api.py
