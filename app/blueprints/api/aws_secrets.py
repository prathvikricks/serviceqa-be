"""Central AWS Secrets Manager.

One set of global AWS credentials (Admin → Settings) lists and reads every
secret in an AWS account. Admins map AWS secrets to projects; developers see
the ones mapped to projects they belong to and reveal them — the value is
fetched LIVE from AWS on every reveal, never stored here.

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
from ...models.project_aws_secret import ProjectAwsSecret
from ...services import secrets_manager
from . import api_bp
from .helpers import _get_or_404, strip

logger = logging.getLogger(__name__)


def _display_key_from_name(name):
    """A display key from an AWS secret name — last path segment, capped at 100.

    AWS names are often paths ("myapp/prod/db-password" → "db-password")."""
    seg = (name or '').rstrip('/').split('/')[-1].strip()
    return seg[:100]


def _region_from_arn(arn):
    """AWS ARN: arn:aws:secretsmanager:<region>:<acct>:secret:<name>. Returns
    the region field, or '' if the ARN isn't shaped as expected."""
    parts = (arn or '').split(':')
    return parts[3] if len(parts) > 3 else ''


def _mappings_for_arn(arn):
    """All project mappings for one AWS secret, for the central/detail views."""
    rows = ProjectAwsSecret.query.filter_by(aws_arn=arn).all()
    return [{
        'assoc_id': a.id, 'project_id': a.project_id,
        'project_name': a.project.name if a.project else None,
        'scope': a.scope_label,
    } for a in rows]


def aws_secret_dict(assoc, can_reveal=False):
    """One mapping as seen through a project. ``id`` is the mapping id — what
    reveal and dissociate operate on."""
    return {
        'id': assoc.id,
        'project_id': assoc.project_id,
        'environment_id': assoc.environment_id,
        'scope': assoc.scope_label,
        'key': assoc.display_key,
        'aws_name': assoc.aws_name,
        'aws_region': assoc.aws_region,
        'aws': True,
        'can_reveal': can_reveal,
    }


def _resolve_environment(project, raw):
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
# Admin — central live list + reveal
# ---------------------------------------------------------------------------

@api_bp.route('/admin/aws-secrets')
@login_required
@admin_required
def list_aws_secrets():
    """Every secret in the AWS account (live), annotated with where each is
    already mapped. Optional ?region= to browse a region other than the default."""
    if not secrets_manager.is_enabled():
        return jsonify({'error': 'AWS Secrets Manager is not configured. Set the '
                                 'credentials in Admin → Settings → AWS Secrets Manager.',
                        'configured': False}), 409

    region = strip(request.args.get('region')) or secrets_manager.default_region()
    try:
        entries = secrets_manager.get_manager().list_all_secrets(region)
    except Exception as exc:
        logger.warning('AWS secrets list failed: %s', exc)
        return jsonify({'error': f'Could not read AWS Secrets Manager: {exc}'}), 502

    # Annotate each AWS secret with its existing mappings across all projects.
    by_arn = {}
    for a in (ProjectAwsSecret.query.join(Project,
              ProjectAwsSecret.project_id == Project.id).all()):
        by_arn.setdefault(a.aws_arn, []).append({
            'assoc_id': a.id, 'project_id': a.project_id,
            'project_name': a.project.name if a.project else None,
            'scope': a.scope_label,
        })

    return jsonify({
        'region': region,
        'aws_secrets': [{
            'aws_arn': e['arn'],
            'aws_name': e['name'],
            'aws_region': _region_from_arn(e['arn']) or region,
            'description': e['description'],
            'mappings': by_arn.get(e['arn'], []),
        } for e in entries],
    })


@api_bp.route('/admin/aws-secrets/reveal', methods=['POST'])
@login_required
@admin_required
def reveal_aws_secret_admin():
    """Reveal any AWS secret's value on the central manager page."""
    data = request.get_json(silent=True) or {}
    aws_arn = strip(data.get('aws_arn'))
    if not aws_arn:
        return jsonify({'error': 'aws_arn is required.'}), 400
    region = strip(data.get('aws_region')) or _region_from_arn(aws_arn) or None

    AuditLog.log('aws_secret_revealed', 'aws_secret', None,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'aws_arn': aws_arn, 'via': 'catalog'})
    try:
        value = secrets_manager.get_manager().get_secret_string(aws_arn, region=region)
    except secrets_manager.SecretsManagerUnavailable as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        logger.warning('AWS secret reveal failed for %s: %s', aws_arn, exc)
        return jsonify({'error': f'Could not read the secret from AWS: {exc}'}), 502
    return jsonify({'aws_arn': aws_arn, 'value': value})


@api_bp.route('/admin/aws-secrets/detail')
@login_required
@admin_required
def aws_secret_detail():
    """One AWS secret's metadata (no value) plus where it's mapped."""
    aws_arn = strip(request.args.get('arn'))
    if not aws_arn:
        return jsonify({'error': 'arn is required.'}), 400
    if not secrets_manager.is_enabled():
        return jsonify({'error': 'AWS Secrets Manager is not configured.',
                        'configured': False}), 409
    region = strip(request.args.get('region')) or _region_from_arn(aws_arn) or None
    try:
        meta = secrets_manager.get_manager().describe_secret(aws_arn, region=region)
    except Exception as exc:
        logger.warning('AWS describe failed for %s: %s', aws_arn, exc)
        return jsonify({'error': f'Could not read the secret from AWS: {exc}'}), 502
    return jsonify({
        'aws_arn': meta['arn'] or aws_arn,
        'aws_name': meta['name'],
        'aws_region': _region_from_arn(meta['arn'] or aws_arn) or region,
        'description': meta['description'],
        'last_changed': meta['last_changed'],
        'mappings': _mappings_for_arn(meta['arn'] or aws_arn),
    })


@api_bp.route('/admin/aws-secrets', methods=['POST'])
@login_required
@admin_required
def create_aws_secret():
    """Create a brand-new secret in AWS Secrets Manager."""
    if not secrets_manager.is_enabled():
        return jsonify({'error': 'AWS Secrets Manager is not configured.',
                        'configured': False}), 409
    data = request.get_json(silent=True) or {}
    name = strip(data.get('name'))
    value = data.get('value') or ''
    description = strip(data.get('description')) or None
    region = strip(data.get('region')) or secrets_manager.default_region()
    if not name:
        return jsonify({'error': 'A name is required.'}), 400
    if not value:
        return jsonify({'error': 'A value is required.'}), 400

    try:
        created = secrets_manager.get_manager().create_secret(
            name, value, description=description, region=region)
    except Exception as exc:
        logger.warning('AWS create secret failed for %s: %s', name, exc)
        return jsonify({'error': f'Could not create the secret in AWS: {exc}'}), 502

    AuditLog.log('aws_secret_created', 'aws_secret', None,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'aws_name': created['name']})
    return jsonify({'aws_arn': created['arn'], 'aws_name': created['name'],
                    'aws_region': region}), 201


@api_bp.route('/admin/aws-secrets', methods=['PUT'])
@login_required
@admin_required
def update_aws_secret():
    """Edit an existing secret's value and/or description in AWS.

    Blank ``value`` leaves the value unchanged (same convention as project
    secrets); ``description`` is written whenever the key is present."""
    if not secrets_manager.is_enabled():
        return jsonify({'error': 'AWS Secrets Manager is not configured.',
                        'configured': False}), 409
    data = request.get_json(silent=True) or {}
    aws_arn = strip(data.get('aws_arn'))
    if not aws_arn:
        return jsonify({'error': 'aws_arn is required.'}), 400
    region = strip(data.get('region')) or _region_from_arn(aws_arn) or None
    value = data.get('value')
    has_description = 'description' in data

    if not value and not has_description:
        return jsonify({'error': 'Nothing to update.'}), 400

    manager = secrets_manager.get_manager()
    try:
        if value:
            manager.put_secret_value(aws_arn, value, region=region)
            AuditLog.log('aws_secret_value_updated', 'aws_secret', None,
                         user_id=current_user.id, ip_address=request.remote_addr,
                         details={'aws_arn': aws_arn})
        if has_description:
            manager.update_secret_description(
                aws_arn, strip(data.get('description')), region=region)
            AuditLog.log('aws_secret_description_updated', 'aws_secret', None,
                         user_id=current_user.id, ip_address=request.remote_addr,
                         details={'aws_arn': aws_arn})
        meta = manager.describe_secret(aws_arn, region=region)
    except Exception as exc:
        logger.warning('AWS update failed for %s: %s', aws_arn, exc)
        return jsonify({'error': f'Could not update the secret in AWS: {exc}'}), 502

    return jsonify({
        'aws_arn': meta['arn'] or aws_arn,
        'aws_name': meta['name'],
        'aws_region': _region_from_arn(meta['arn'] or aws_arn) or region,
        'description': meta['description'],
        'last_changed': meta['last_changed'],
        'mappings': _mappings_for_arn(meta['arn'] or aws_arn),
    })


# ---------------------------------------------------------------------------
# Admin — associate / dissociate to a project
# ---------------------------------------------------------------------------

@api_bp.route('/admin/projects/<int:pid>/aws-secrets', methods=['POST'])
@login_required
@admin_required
def associate_aws_secret(pid):
    project = _get_or_404(Project, pid)
    data = request.get_json(silent=True) or {}

    aws_arn = strip(data.get('aws_arn'))
    aws_name = strip(data.get('aws_name')) or aws_arn
    if not aws_arn:
        return jsonify({'error': 'aws_arn is required.'}), 400

    aws_region = strip(data.get('aws_region')) or _region_from_arn(aws_arn) \
        or secrets_manager.default_region()
    display_key = strip(data.get('display_key')) or _display_key_from_name(aws_name)
    if not display_key:
        return jsonify({'error': 'Could not derive a key from the secret name.'}), 400
    if len(display_key) > 100:
        return jsonify({'error': 'Key must be at most 100 characters.'}), 400

    environment_id, error = _resolve_environment(project, data.get('environment_id'))
    if error:
        return jsonify({'error': error}), 400

    exists = ProjectAwsSecret.query.filter_by(
        project_id=pid, environment_id=environment_id, aws_arn=aws_arn).first()
    if exists is not None:
        return jsonify({'error': 'That AWS secret is already mapped to this scope.'}), 400

    assoc = ProjectAwsSecret(
        project_id=pid, environment_id=environment_id, aws_arn=aws_arn,
        aws_name=aws_name, aws_region=aws_region, display_key=display_key,
        created_by=current_user.id)
    db.session.add(assoc)
    db.session.commit()
    AuditLog.log('aws_secret_associated', 'aws_secret', assoc.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'aws_name': aws_name, 'project_id': pid,
                          'scope': assoc.scope_label})
    return jsonify(aws_secret_dict(assoc, can_reveal=True)), 201


@api_bp.route('/admin/projects/<int:pid>/aws-secrets/<int:aid>', methods=['DELETE'])
@login_required
@admin_required
def dissociate_aws_secret(pid, aid):
    _get_or_404(Project, pid)
    assoc = _get_or_404(ProjectAwsSecret, aid)
    if assoc.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404

    name = assoc.aws_name
    db.session.delete(assoc)
    db.session.commit()
    AuditLog.log('aws_secret_dissociated', 'aws_secret', aid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'aws_name': name, 'project_id': pid})
    return jsonify({'dissociated': True, 'id': aid})


# ---------------------------------------------------------------------------
# Readers — scoped to project membership
# ---------------------------------------------------------------------------

@api_bp.route('/projects/<int:pid>/aws-secrets')
@login_required
def list_project_aws_secrets(pid):
    """AWS secrets mapped to a project, metadata only (no values)."""
    _get_or_404(Project, pid)
    if not current_user.is_member_of(pid):
        return jsonify({'error': 'Access denied'}), 403

    can_reveal = current_user.can_view_secrets_of(pid)
    rows = (ProjectAwsSecret.query.filter_by(project_id=pid)
            .order_by(ProjectAwsSecret.display_key).all())
    return jsonify({
        'aws_secrets': [aws_secret_dict(r, can_reveal) for r in rows],
        'can_reveal': can_reveal,
    })


@api_bp.route('/projects/<int:pid>/aws-secrets/<int:aid>/reveal', methods=['POST'])
@login_required
def reveal_project_aws_secret(pid, aid):
    """Reveal a mapped AWS secret's value through a project it belongs to.

    The value is fetched live from AWS every time — nothing is stored here."""
    _get_or_404(Project, pid)
    assoc = _get_or_404(ProjectAwsSecret, aid)
    if assoc.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404
    if not current_user.can_view_secrets_of(pid):
        return jsonify({'error': 'You do not have permission to view this secret.'}), 403

    # Audit the attempt before the fetch — a reveal that fails at AWS still
    # happened, and the failure itself is worth recording.
    AuditLog.log('aws_secret_revealed', 'aws_secret', assoc.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'aws_name': assoc.aws_name, 'project_id': pid, 'via': 'project'})
    try:
        value = secrets_manager.get_manager().get_secret_string(
            assoc.aws_arn, region=assoc.aws_region)
    except secrets_manager.SecretsManagerUnavailable as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        logger.warning('AWS secret reveal failed for %s: %s', assoc.aws_arn, exc)
        return jsonify({'error': f'Could not read the secret from AWS: {exc}'}), 502
    return jsonify({'id': assoc.id, 'key': assoc.display_key, 'value': value})
