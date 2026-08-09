"""Small shared helpers for the JSON API."""
from flask import abort

from ...extensions import db


def _get_or_404(model, pk):
    """Fetch a row by primary key or abort with the API's JSON 404."""
    obj = db.session.get(model, pk)
    if obj is None:
        abort(404)
    return obj


def strip(value):
    """Trim a form value, tolerating non-strings (returns '')."""
    return value.strip() if isinstance(value, str) else ''
