"""Gunicorn entrypoint.

Run with a single worker: the scheduler lives in-process, so each extra worker
would arm its own copy of every start/stop job. Scale with threads instead
(see entrypoint.sh).
"""
from app import create_app, start_background_jobs

app = create_app('production')
start_background_jobs(app)
