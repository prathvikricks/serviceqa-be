#!/bin/sh
set -e

# Alembic owns schema changes once migrations/ exists; until then seed.py's
# create_all() builds the tables. Skipping cleanly keeps a fresh clone bootable.
if [ -d migrations ]; then
    echo "==> Running database migrations..."
    flask --app "app:create_app('production')" db upgrade
else
    echo "==> No migrations/ directory — schema comes from create_all()."
fi

echo "==> Seeding..."
if [ -n "$SEED_DEMO" ]; then
    python3 seed.py --demo
else
    python3 seed.py
fi

echo "==> Starting Gunicorn on port ${PORT:-5000}..."
# One worker on purpose: APScheduler runs in-process, so a second worker would
# arm a duplicate of every start/stop job. Scale with threads.
exec gunicorn \
    --bind "0.0.0.0:${PORT:-5000}" \
    --workers "${GUNICORN_WORKERS:-1}" \
    --threads "${GUNICORN_THREADS:-8}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    wsgi:app
