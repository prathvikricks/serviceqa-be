"""Project secrets.

Two audiences, deliberately separated:

* **Readers** — anyone who can see the project lists its secrets, but the list
  carries metadata only. Plaintext comes from one endpoint, ``/reveal``, which
  requires the per-membership permission and writes an audit entry every time.
* **Admins** — create, update and delete.

Nothing here ever puts a secret value into a log line or an audit ``details``
blob. The only place plaintext leaves the process is the ``/reveal`` response.
"""
import logging
from datetime import datetime, timezone

from flask import jsonify, request
from flask_login import login_required, current_user

from ...extensions import db
from ...decorators import admin_required
from ...models.audit import AuditLog
from ...models.environment import Environment
from ...models.project import Project
from ...models.secret import ProjectSecret
from . import api_bp
from .helpers import _get_or_404, strip

logger = logging.getLogger(__name__)


def secret_dict(secret, can_reveal=False):
    """Metadata only — never the value. `can_reveal` tells the UI whether to
    offer the button, and is NOT itself the authorization check."""
    return {
        'id': secret.id,
        'project_id': secret.project_id,
        'environment_id': secret.environment_id,
        'scope': secret.scope_label,
        'key': secret.key,
        'description': secret.description,
        'created_by': secret.creator.username if secret.creator else None,
        'created_at': secret.created_at.isoformat() if secret.created_at else None,
        'updated_at': secret.updated_at.isoformat() if secret.updated_at else None,
        'source': secret.source,
        'synced_at': secret.synced_at.isoformat() if secret.synced_at else None,
        'can_reveal': can_reveal,
    }


def _secret_key_from_name(name):
    """A ProjectSecret key from an AWS secret name.

    AWS names are often paths ("myapp/prod/db-password"); take the last segment
    and cap it at the column's 100 chars. Returns '' when nothing usable is left.
    """
    seg = (name or '').rstrip('/').split('/')[-1].strip()
    return seg[:100]


def _resolve_environment(project, raw):
    """Validate an optional environment_id. Returns (environment_id, error).

    A secret may only be pinned to an environment of its OWN project —
    otherwise an admin could attach one project's credential to another's.
    """
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


def _duplicate_key(project_id, environment_id, key, exclude_id=None):
    """The unique constraint can't catch project-wide duplicates, because SQL
    treats NULL environment_id values as distinct. Check explicitly."""
    query = ProjectSecret.query.filter_by(
        project_id=project_id, environment_id=environment_id, key=key)
    if exclude_id is not None:
        query = query.filter(ProjectSecret.id != exclude_id)
    return query.first() is not None


# ---------------------------------------------------------------------------
# Readers — scoped to project membership
# ---------------------------------------------------------------------------

@api_bp.route('/projects/<int:pid>/secrets')
@login_required
def list_secrets(pid):
    """Secrets for a project, WITHOUT values.

    Visible to anyone who can see the project; revealing is a separate step.
    """
    _get_or_404(Project, pid)
    if not current_user.is_member_of(pid):
        return jsonify({'error': 'Access denied'}), 403

    can_reveal = current_user.can_view_secrets_of(pid)
    secrets = (ProjectSecret.query.filter_by(project_id=pid)
               .order_by(ProjectSecret.key).all())
    return jsonify({
        'secrets': [secret_dict(s, can_reveal) for s in secrets],
        'can_reveal': can_reveal,
    })


@api_bp.route('/projects/<int:pid>/secrets/<int:sid>/reveal', methods=['POST'])
@login_required
def reveal_secret(pid, sid):
    """Return one secret's plaintext value.

    POST rather than GET on purpose: a GET would put the secret's identity into
    browser history, proxy access logs and Referer headers.
    """
    _get_or_404(Project, pid)
    secret = _get_or_404(ProjectSecret, sid)
    if secret.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404

    if not current_user.can_view_secrets_of(pid):
        # Same message whether they're a non-member or a member without the
        # permission — no need to tell them which.
        return jsonify({'error': 'You do not have permission to view this secret.'}), 403

    # Audit BEFORE returning: if the write fails, the read doesn't happen
    # unrecorded. The value itself never enters the audit row.
    AuditLog.log('secret_revealed', 'secret', secret.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': secret.key, 'project_id': pid,
                          'scope': secret.scope_label})

    return jsonify({'id': secret.id, 'key': secret.key, 'value': secret.get_value()})


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------

@api_bp.route('/admin/projects/<int:pid>/secrets', methods=['POST'])
@login_required
@admin_required
def create_secret(pid):
    project = _get_or_404(Project, pid)
    data = request.get_json(silent=True) or {}

    key = strip(data.get('key'))
    value = data.get('value') or ''
    if not key:
        return jsonify({'error': 'Key is required.'}), 400
    if len(key) > 100:
        return jsonify({'error': 'Key must be at most 100 characters.'}), 400
    if not value:
        return jsonify({'error': 'Value is required.'}), 400

    environment_id, error = _resolve_environment(project, data.get('environment_id'))
    if error:
        return jsonify({'error': error}), 400
    if _duplicate_key(pid, environment_id, key):
        return jsonify({'error': f'"{key}" already exists for that scope.'}), 400

    secret = ProjectSecret(
        project_id=pid,
        environment_id=environment_id,
        key=key,
        description=strip(data.get('description')) or None,
        created_by=current_user.id,
    )
    secret.set_value(value)
    db.session.add(secret)
    db.session.commit()

    AuditLog.log('secret_created', 'secret', secret.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': secret.key, 'project_id': pid})
    return jsonify(secret_dict(secret, can_reveal=True)), 201


@api_bp.route('/admin/projects/<int:pid>/secrets/<int:sid>', methods=['PUT'])
@login_required
@admin_required
def update_secret(pid, sid):
    project = _get_or_404(Project, pid)
    secret = _get_or_404(ProjectSecret, sid)
    if secret.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404

    data = request.get_json(silent=True) or {}
    key = strip(data.get('key')) or secret.key
    if len(key) > 100:
        return jsonify({'error': 'Key must be at most 100 characters.'}), 400

    environment_id, error = _resolve_environment(project, data.get('environment_id'))
    if error:
        return jsonify({'error': error}), 400
    if _duplicate_key(pid, environment_id, key, exclude_id=secret.id):
        return jsonify({'error': f'"{key}" already exists for that scope.'}), 400

    secret.key = key
    secret.environment_id = environment_id
    if 'description' in data:
        secret.description = strip(data.get('description')) or None

    # The edit form never receives the value back, so blank means "unchanged" —
    # same convention as cloud credentials in admin._build_provider_config.
    value = data.get('value')
    if value:
        secret.set_value(value)

    db.session.commit()
    AuditLog.log('secret_updated', 'secret', secret.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': secret.key, 'project_id': pid,
                          'value_changed': bool(value)})
    return jsonify(secret_dict(secret, can_reveal=True))


@api_bp.route('/admin/projects/<int:pid>/secrets/<int:sid>', methods=['DELETE'])
@login_required
@admin_required
def delete_secret(pid, sid):
    _get_or_404(Project, pid)
    secret = _get_or_404(ProjectSecret, sid)
    if secret.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404

    key = secret.key
    db.session.delete(secret)
    db.session.commit()
    AuditLog.log('secret_deleted', 'secret', sid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'key': key, 'project_id': pid})
    return jsonify({'deleted': True, 'id': sid})


# ---------------------------------------------------------------------------
# AWS Secrets Manager sync
# ---------------------------------------------------------------------------

# The tag AWS secrets must carry to be claimed by a project. Value = project.slug.
SYNC_TAG_KEY = 'Project'


@api_bp.route('/admin/projects/<int:pid>/secrets/sync', methods=['POST'])
@login_required
@admin_required
def sync_secrets(pid):
    """Pull AWS Secrets Manager secrets tagged ``Project=<slug>`` into this
    project's secrets, using the project's own AWS credentials.

    Upsert, never destructive: only rows this sync created (``source='aws'``)
    are updated; a manually-created secret sharing a key is left alone and
    reported as skipped. Secrets removed from AWS are counted, not deleted, so
    an admin decides whether to remove them.
    """
    project = _get_or_404(Project, pid)
    if project.cloud_provider != 'aws':
        return jsonify({'error': 'Secret sync is only available for AWS projects.'}), 400
    if (project.mode or 'mock') != 'real':
        return jsonify({'error': 'Switch the project to Real mode with AWS '
                                 'credentials before syncing.'}), 400

    from ...services.aws_manager import AWSManager
    manager = AWSManager(project.get_provider_config())

    try:
        entries = manager.list_secrets_by_tag(SYNC_TAG_KEY, project.slug)
    except Exception as exc:  # boto/cred/permission failure — report, don't 500.
        logger.warning('Secret sync list failed for project %s: %s', pid, exc)
        return jsonify({'error': f'Could not read AWS Secrets Manager: {exc}'}), 502

    created, updated, skipped, seen_arns = 0, 0, [], []
    now = datetime.now(timezone.utc)

    for entry in entries:
        arn = entry['arn']
        key = _secret_key_from_name(entry['name'])
        if not key:
            skipped.append({'key': entry['name'], 'reason': 'unusable secret name'})
            continue

        try:
            value = manager.get_secret_string(arn)
        except Exception as exc:
            logger.warning('Secret sync fetch failed for %s: %s', arn, exc)
            skipped.append({'key': key, 'reason': 'could not read value'})
            continue

        seen_arns.append(arn)
        # Match project-wide (environment_id NULL) by key.
        existing = ProjectSecret.query.filter_by(
            project_id=pid, environment_id=None, key=key).first()

        if existing is None:
            secret = ProjectSecret(
                project_id=pid, environment_id=None, key=key,
                description=entry['description'] or 'Synced from AWS Secrets Manager',
                source='aws', external_id=arn, synced_at=now,
                created_by=current_user.id)
            secret.set_value(value)
            db.session.add(secret)
            created += 1
        elif existing.source == 'aws':
            existing.set_value(value)
            existing.external_id = arn
            existing.synced_at = now
            if entry['description']:
                existing.description = entry['description']
            updated += 1
        else:
            skipped.append({'key': key,
                            'reason': 'a manually-created secret with this key exists'})

    # AWS-sourced rows whose ARN wasn't seen this run no longer exist upstream.
    stale_q = ProjectSecret.query.filter_by(project_id=pid, source='aws')
    if seen_arns:
        stale_q = stale_q.filter(
            ProjectSecret.external_id.isnot(None),
            ProjectSecret.external_id.notin_(seen_arns))
    missing_in_aws = stale_q.count()

    db.session.commit()

    # Counts only — never a key's value.
    AuditLog.log('secrets_synced', 'project', pid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'project_id': pid, 'tag': f'{SYNC_TAG_KEY}={project.slug}',
                          'created': created, 'updated': updated,
                          'skipped': len(skipped), 'missing_in_aws': missing_in_aws})

    return jsonify({
        'created': created,
        'updated': updated,
        'skipped': skipped,
        'missing_in_aws': missing_in_aws,
        'tag': f'{SYNC_TAG_KEY}={project.slug}',
        'region': manager.region,
    })
