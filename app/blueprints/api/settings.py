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


def _invalidate_caches(key):
    """A changed credential must take effect now, not when the token expires."""
    if key.startswith('GRAPH_'):
        from ...services import graph_mail
        graph_mail.reset_token_cache()


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
    return jsonify({
        'settings': [_describe(k, m) for k, m in Setting.EDITABLE.items()],
        'groups': Setting.GROUPS,
    })


@api_bp.route('/admin/settings', methods=['PUT'])
@login_required
@admin_required
def settings_update_many():
    """Save a whole integration at once.

    Four Microsoft credentials come from one app registration in one sitting;
    saving them one at a time means the feature briefly looks configured with a
    tenant id and no secret. Validation happens for every key before anything is
    written, so a typo saves nothing rather than half of it.
    """
    values = (request.get_json(silent=True) or {}).get('values')
    if not isinstance(values, dict) or not values:
        return jsonify({'error': 'No values supplied.'}), 400

    unknown = [k for k in values if k not in Setting.EDITABLE]
    if unknown:
        return jsonify({'error': f'Cannot edit: {", ".join(sorted(unknown))}.'}), 400

    changed = []
    for key, raw in values.items():
        value = ('' if raw is None else str(raw)).strip()
        row = Setting.query.filter_by(key=key).first()

        if not value:
            # Empty clears, matching DELETE — the env var takes over again.
            if row is not None:
                db.session.delete(row)
                changed.append(key)
            continue

        if row is None:
            row = Setting(key=key)
            db.session.add(row)
        row.set_value(value)
        row.updated_by = current_user.id
        changed.append(key)

    db.session.commit()
    for key in changed:
        _invalidate_caches(key)

    if changed:
        AuditLog.log('setting_updated', 'setting', None,
                     user_id=current_user.id, ip_address=request.remote_addr,
                     details={'keys': sorted(changed)})

    return jsonify({
        'settings': [_describe(k, m) for k, m in Setting.EDITABLE.items()],
        'groups': Setting.GROUPS,
    })


@api_bp.route('/admin/settings/status')
@login_required
@admin_required
def settings_status():
    """What each integration is actually doing, not just whether it is filled in."""
    from ...services import chat_agent, graph_mail, ticket_intake

    llm = {'configured': chat_agent.is_enabled(), 'model': chat_agent.model_name()}

    mail = {'configured': graph_mail.is_enabled(),
            'mailbox': current_app.config.get('DEVOPS_MAILBOX'),
            'reachable': False, 'error': None}
    if mail['configured']:
        try:
            result = ticket_intake.check_connection()
            mail['reachable'] = True
            mail['mailbox'] = result.get('mailbox')
        except Exception as exc:
            # A tile that says "configured" while Graph refuses is exactly the
            # failure this endpoint exists to surface.
            mail['error'] = str(exc)[:300]

    return jsonify({'llm': llm, 'mail': mail})


@api_bp.route('/admin/settings/llm/models')
@login_required
@admin_required
def settings_llm_models():
    """The models this key can use. Failing here is the bad-key signal."""
    from ...services import chat_agent

    try:
        return jsonify({'models': chat_agent.list_models()})
    except chat_agent.AgentUnavailable as exc:
        return jsonify({'error': str(exc)}), 503
    except chat_agent.AgentError as exc:
        return jsonify({'error': str(exc)}), 502


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
    _invalidate_caches(key)

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
        _invalidate_caches(key)
        AuditLog.log('setting_cleared', 'setting', None,
                     user_id=current_user.id, ip_address=request.remote_addr,
                     details={'key': key})
    return jsonify(_describe(key, meta))
