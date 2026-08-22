"""Admin-editable application settings.

Only the keys declared in Setting.EDITABLE can be written. An open key/value
store reachable over HTTP is a way to overwrite SECRET_KEY by accident, or on
purpose.

Values are never returned. A read tells you whether a key is set, where it came
from, and a four-character tail so an admin can tell two keys apart — nothing
more. Writing one is audited; the value never reaches the audit details.
"""
import logging

from flask import current_app, jsonify, request
from flask_login import login_required, current_user

from ...decorators import admin_required
from ...extensions import db
from ...models.audit import AuditLog
from ...models.setting import Setting
from . import api_bp

logger = logging.getLogger(__name__)


def _describe(key, meta):
    row = Setting.query.filter_by(key=key).first()
    stored = row.get_value() if row else ''
    env_value = current_app.config.get(key)

    return {
        'key': key,
        'label': meta['label'],
        'help': meta.get('help'),
        'secret': meta.get('secret', True),
        'is_set': bool(stored or env_value),
        # Where the effective value comes from. Worth surfacing: an admin who
        # clears a key here needs to know the environment may still supply one.
        'source': 'settings' if stored else ('environment' if env_value else None),
        'hint': row.hint if (row and stored) else None,
        'updated_by': row.editor.username if (row and row.editor) else None,
        'updated_at': row.updated_at.isoformat() if row and row.updated_at else None,
    }


@api_bp.route('/admin/settings')
@login_required
@admin_required
def settings_list():
    return jsonify({'settings': [_describe(k, m) for k, m in Setting.EDITABLE.items()]})


@api_bp.route('/admin/settings/<key>', methods=['PUT'])
@login_required
@admin_required
def settings_update(key):
    meta = Setting.EDITABLE.get(key)
    if meta is None:
        return jsonify({'error': 'That setting cannot be edited here.'}), 400

    value = ((request.get_json(silent=True) or {}).get('value') or '').strip()
    if not value:
        return jsonify({'error': 'A value is required. Use DELETE to clear it.'}), 400

    row = Setting.query.filter_by(key=key).first()
    if row is None:
        row = Setting(key=key)
        db.session.add(row)
    row.set_value(value)
    row.updated_by = current_user.id
    db.session.commit()

    # Never the value, not even truncated beyond the display hint.
    AuditLog.log('setting_updated', 'setting', row.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': key})
    return jsonify(_describe(key, meta))


@api_bp.route('/admin/settings/<key>', methods=['DELETE'])
@login_required
@admin_required
def settings_clear(key):
    meta = Setting.EDITABLE.get(key)
    if meta is None:
        return jsonify({'error': 'That setting cannot be edited here.'}), 400

    row = Setting.query.filter_by(key=key).first()
    if row is not None:
        db.session.delete(row)
        db.session.commit()
        AuditLog.log('setting_cleared', 'setting', None,
                     user_id=current_user.id, ip_address=request.remote_addr,
                     details={'key': key})
    return jsonify(_describe(key, meta))
