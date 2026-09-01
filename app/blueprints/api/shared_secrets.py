"""Shared secret catalog.

A SharedSecret is defined once and attached to many projects — one source of
truth, so editing its value changes it everywhere it's attached. Two audiences,
same split as project secrets:

* **Admins** — manage the catalog (CRUD) and attach/detach secrets to projects.
* **Readers** — a project member lists the shared secrets attached to a project
  they belong to, and reveals a value only with the per-membership permission.

As with project secrets, no value ever enters a log line or an audit ``details``
blob. Plaintext leaves the process only through the two ``/reveal`` endpoints.
"""
import logging

from flask import jsonify, request
from flask_login import login_required, current_user

from ...extensions import db
from ...decorators import admin_required
from ...models.audit import AuditLog
from ...models.environment import Environment
from ...models.project import Project
from ...models.shared_secret import SharedSecret, SharedSecretAttachment
from . import api_bp
from .helpers import _get_or_404, strip

logger = logging.getLogger(__name__)


def shared_secret_dict(ss):
    """Catalog metadata — never the value."""
    return {
        'id': ss.id,
        'key': ss.key,
        'description': ss.description,
        'created_by': ss.creator.username if ss.creator else None,
        'created_at': ss.created_at.isoformat() if ss.created_at else None,
        'updated_at': ss.updated_at.isoformat() if ss.updated_at else None,
        'attachment_count': ss.attachments.count(),
    }


def attachment_dict(att, can_reveal=False):
    """One shared secret as seen through a project it's attached to. The ``id``
    is the ATTACHMENT id — what reveal and detach operate on."""
    ss = att.shared_secret
    return {
        'id': att.id,
        'shared_secret_id': ss.id,
        'project_id': att.project_id,
        'environment_id': att.environment_id,
        'scope': att.scope_label,
        'key': ss.key,
        'description': ss.description,
        'created_by': ss.creator.username if ss.creator else None,
        'attached_at': att.created_at.isoformat() if att.created_at else None,
        'shared': True,
        'can_reveal': can_reveal,
    }


def _resolve_project_environment(project, raw):
    """Validate an optional environment_id belongs to this project."""
    if raw in (None, '', 'null'):
        return None, None
    try:
        eid = int(raw)
    except (TypeError, ValueError):
        return None, 'environment_id must be a number.'
    env = db.session.get(Environment, eid)
    if env is None or env.project_id != project.id:
        return None, 'That environment does not belong to this project.'
    return eid, None


# ---------------------------------------------------------------------------
# Admin — catalog CRUD
# ---------------------------------------------------------------------------

@api_bp.route('/admin/shared-secrets')
@login_required
@admin_required
def list_shared_secrets():
    secrets = SharedSecret.query.order_by(SharedSecret.key).all()
    return jsonify({'shared_secrets': [shared_secret_dict(s) for s in secrets]})


@api_bp.route('/admin/shared-secrets', methods=['POST'])
@login_required
@admin_required
def create_shared_secret():
    data = request.get_json(silent=True) or {}
    key = strip(data.get('key'))
    value = data.get('value') or ''
    if not key:
        return jsonify({'error': 'Key is required.'}), 400
    if len(key) > 100:
        return jsonify({'error': 'Key must be at most 100 characters.'}), 400
    if not value:
        return jsonify({'error': 'Value is required.'}), 400
    if SharedSecret.query.filter_by(key=key).first() is not None:
        return jsonify({'error': f'A shared secret named "{key}" already exists.'}), 400

    ss = SharedSecret(key=key, description=strip(data.get('description')) or None,
                      created_by=current_user.id)
    ss.set_value(value)
    db.session.add(ss)
    db.session.commit()
    AuditLog.log('shared_secret_created', 'shared_secret', ss.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': ss.key})
    return jsonify(shared_secret_dict(ss)), 201


@api_bp.route('/admin/shared-secrets/<int:sid>', methods=['PUT'])
@login_required
@admin_required
def update_shared_secret(sid):
    ss = _get_or_404(SharedSecret, sid)
    data = request.get_json(silent=True) or {}

    key = strip(data.get('key')) or ss.key
    if len(key) > 100:
        return jsonify({'error': 'Key must be at most 100 characters.'}), 400
    clash = SharedSecret.query.filter(SharedSecret.key == key,
                                      SharedSecret.id != ss.id).first()
    if clash is not None:
        return jsonify({'error': f'A shared secret named "{key}" already exists.'}), 400

    ss.key = key
    if 'description' in data:
        ss.description = strip(data.get('description')) or None
    # Blank value means "unchanged" — same convention as project secrets.
    value = data.get('value')
    if value:
        ss.set_value(value)

    db.session.commit()
    AuditLog.log('shared_secret_updated', 'shared_secret', ss.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': ss.key, 'value_changed': bool(value)})
    return jsonify(shared_secret_dict(ss))


@api_bp.route('/admin/shared-secrets/<int:sid>', methods=['DELETE'])
@login_required
@admin_required
def delete_shared_secret(sid):
    ss = _get_or_404(SharedSecret, sid)
    key = ss.key
    db.session.delete(ss)  # cascades to attachments
    db.session.commit()
    AuditLog.log('shared_secret_deleted', 'shared_secret', sid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': key})
    return jsonify({'deleted': True, 'id': sid})


@api_bp.route('/admin/shared-secrets/<int:sid>/reveal', methods=['POST'])
@login_required
@admin_required
def reveal_shared_secret_admin(sid):
    """Reveal a catalog secret's value on the admin catalog page."""
    ss = _get_or_404(SharedSecret, sid)
    AuditLog.log('shared_secret_revealed', 'shared_secret', ss.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': ss.key, 'via': 'catalog'})
    return jsonify({'id': ss.id, 'key': ss.key, 'value': ss.get_value()})


# ---------------------------------------------------------------------------
# Admin — attach / detach to a project
# ---------------------------------------------------------------------------

@api_bp.route('/admin/projects/<int:pid>/shared-secrets', methods=['POST'])
@login_required
@admin_required
def attach_shared_secret(pid):
    project = _get_or_404(Project, pid)
    data = request.get_json(silent=True) or {}

    try:
        shared_secret_id = int(data.get('shared_secret_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'shared_secret_id is required.'}), 400
    ss = db.session.get(SharedSecret, shared_secret_id)
    if ss is None:
        return jsonify({'error': 'That shared secret does not exist.'}), 404

    environment_id, error = _resolve_project_environment(project, data.get('environment_id'))
    if error:
        return jsonify({'error': error}), 400

    exists = SharedSecretAttachment.query.filter_by(
        shared_secret_id=shared_secret_id, project_id=pid,
        environment_id=environment_id).first()
    if exists is not None:
        return jsonify({'error': 'That shared secret is already attached to this scope.'}), 400

    att = SharedSecretAttachment(
        shared_secret_id=shared_secret_id, project_id=pid,
        environment_id=environment_id, created_by=current_user.id)
    db.session.add(att)
    db.session.commit()
    AuditLog.log('shared_secret_attached', 'shared_secret', ss.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': ss.key, 'project_id': pid})
    return jsonify(attachment_dict(att, can_reveal=True)), 201


@api_bp.route('/admin/projects/<int:pid>/shared-secrets/<int:aid>', methods=['DELETE'])
@login_required
@admin_required
def detach_shared_secret(pid, aid):
    _get_or_404(Project, pid)
    att = _get_or_404(SharedSecretAttachment, aid)
    if att.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404

    key = att.shared_secret.key
    db.session.delete(att)  # detaches only; the catalog secret stays
    db.session.commit()
    AuditLog.log('shared_secret_detached', 'shared_secret', att.shared_secret_id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': key, 'project_id': pid})
    return jsonify({'detached': True, 'id': aid})


# ---------------------------------------------------------------------------
# Readers — scoped to project membership
# ---------------------------------------------------------------------------

@api_bp.route('/projects/<int:pid>/shared-secrets')
@login_required
def list_project_shared_secrets(pid):
    """Shared secrets attached to a project, WITHOUT values."""
    _get_or_404(Project, pid)
    if not current_user.is_member_of(pid):
        return jsonify({'error': 'Access denied'}), 403

    can_reveal = current_user.can_view_secrets_of(pid)
    atts = (SharedSecretAttachment.query.filter_by(project_id=pid)
            .join(SharedSecret).order_by(SharedSecret.key).all())
    return jsonify({
        'shared_secrets': [attachment_dict(a, can_reveal) for a in atts],
        'can_reveal': can_reveal,
    })


@api_bp.route('/projects/<int:pid>/shared-secrets/<int:aid>/reveal', methods=['POST'])
@login_required
def reveal_project_shared_secret(pid, aid):
    """Reveal a shared secret's value through a project it's attached to."""
    _get_or_404(Project, pid)
    att = _get_or_404(SharedSecretAttachment, aid)
    if att.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404

    if not current_user.can_view_secrets_of(pid):
        return jsonify({'error': 'You do not have permission to view this secret.'}), 403

    ss = att.shared_secret
    AuditLog.log('shared_secret_revealed', 'shared_secret', ss.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': ss.key, 'project_id': pid, 'via': 'project'})
    return jsonify({'id': att.id, 'key': ss.key, 'value': ss.get_value()})
