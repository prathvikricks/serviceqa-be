"""Admin CRUD — projects, environments, cloud services, members, users.

Every endpoint here is admin-only. This is where the inventory that requests are
raised against gets defined: a Project (one cloud account + mode) holds
Environments, and each Environment holds the CloudServices that get started and
stopped together.
"""
from flask import jsonify, request
from flask_login import login_required, current_user

from ...extensions import db
from ...decorators import admin_required
from ...models.user import User, Role, ProjectMember
from ...models.project import Project
from ...models.environment import Environment, CloudService
from ...models.request import EnvironmentRequest
from ...models.audit import AuditLog
from . import api_bp
from .helpers import _get_or_404, strip
from .serializers import (project_dict, environment_dict, service_dict,
                          member_dict, user_dict)


CLOUD_PROVIDERS = [{'value': v, 'label': l} for v, l in Project.PROVIDERS]
MODES = [{'value': v, 'label': l} for v, l in Project.MODES]
ROLES = [
    {'value': 'developer', 'label': 'Developer'},
    {'value': 'devops', 'label': 'DevOps'},
    {'value': 'admin', 'label': 'Admin'},
]


def _build_provider_config(provider, data, existing=None):
    """Build a provider_config dict from a JSON body.

    A secret the client didn't re-submit is preserved from `existing` — the UI
    never receives secrets back, so a blank field means "unchanged", not "clear".
    """
    existing = existing or {}

    if provider == 'azure':
        config = {
            'tenant_id': strip(data.get('azure_tenant_id')),
            'client_id': strip(data.get('azure_client_id')),
            'subscription_id': strip(data.get('azure_subscription_id')),
        }
        secret_field, incoming = 'client_secret', strip(data.get('azure_client_secret'))
    else:
        config = {
            'region': strip(data.get('aws_region')) or 'us-east-1',
            'account_id': strip(data.get('aws_account_id')),
            'access_key_id': strip(data.get('aws_access_key_id')),
        }
        secret_field, incoming = 'secret_access_key', strip(data.get('aws_secret_access_key'))

    config[secret_field] = incoming or existing.get(secret_field, '')
    return config


def _provider_config_public(project):
    """Provider config WITHOUT secrets — a secret is reported only as `*_set`."""
    cfg = project.provider_config or {}
    if project.cloud_provider == 'azure':
        return {
            'tenant_id': cfg.get('tenant_id', ''),
            'client_id': cfg.get('client_id', ''),
            'subscription_id': cfg.get('subscription_id', ''),
            'client_secret_set': project.has_secret('client_secret'),
        }
    return {
        'region': cfg.get('region', 'us-east-1'),
        'account_id': cfg.get('account_id', ''),
        'access_key_id': cfg.get('access_key_id', ''),
        'secret_access_key_set': project.has_secret('secret_access_key'),
    }


def _validate_project_body(data, project=None):
    """Shared create/edit validation. Returns (name, provider, mode) or an error string."""
    name = strip(data.get('name'))
    if not name:
        return None, 'Project name is required.'
    if len(name) > 100:
        return None, 'Project name must be at most 100 characters.'

    provider = data.get('cloud_provider')
    if provider not in dict(Project.PROVIDERS):
        return None, 'cloud_provider must be "aws" or "azure".'

    mode = data.get('mode', 'mock')
    if mode not in dict(Project.MODES):
        return None, 'mode must be "mock" or "real".'

    clash = Project.query.filter_by(name=name).first()
    if clash and (project is None or clash.id != project.id):
        return None, 'A project with this name already exists.'

    return (name, provider, mode), None


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@api_bp.route('/admin/projects', methods=['GET'])
@login_required
@admin_required
def admin_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return jsonify({
        'projects': [project_dict(p) for p in projects],
        'cloud_providers': CLOUD_PROVIDERS,
        'modes': MODES,
    })


@api_bp.route('/admin/projects', methods=['POST'])
@login_required
@admin_required
def admin_project_create():
    data = request.get_json(silent=True) or {}
    fields, error = _validate_project_body(data)
    if error:
        return jsonify({'error': error}), 400
    name, provider, mode = fields

    project = Project(
        name=name,
        slug=Project.generate_slug(name),
        description=data.get('description'),
        cloud_provider=provider,
        mode=mode,
        created_by=current_user.id,
    )
    project.set_provider_config(_build_provider_config(provider, data))
    db.session.add(project)
    db.session.commit()

    AuditLog.log('project_created', 'project', project.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'name': project.name, 'provider': provider, 'mode': mode})

    return jsonify(project_dict(project)), 201


@api_bp.route('/admin/projects/<int:pid>', methods=['GET'])
@login_required
@admin_required
def admin_project_detail(pid):
    project = _get_or_404(Project, pid)
    data = project_dict(project, detail=True)
    data['description'] = project.description
    data['provider_config'] = _provider_config_public(project)
    data['environments'] = [
        environment_dict(e) for e in project.environments.order_by(Environment.name).all()
    ]
    data['members'] = [member_dict(m) for m in project.members.all()]
    return jsonify(data)


@api_bp.route('/admin/projects/<int:pid>', methods=['PUT'])
@login_required
@admin_required
def admin_project_edit(pid):
    project = _get_or_404(Project, pid)
    data = request.get_json(silent=True) or {}
    fields, error = _validate_project_body(data, project=project)
    if error:
        return jsonify({'error': error}), 400
    name, provider, mode = fields

    project.name = name
    project.slug = Project.generate_slug(name)
    project.description = data.get('description')
    project.cloud_provider = provider
    project.mode = mode
    project.set_provider_config(_build_provider_config(
        provider, data, existing=project.get_provider_config()))
    db.session.commit()

    # Managers are cached per (mode, provider, project); drop the cache so a
    # credential or mode change takes effect now rather than after a restart.
    from ...services.cloud_manager import CloudManagerFactory
    CloudManagerFactory.clear_cache()

    AuditLog.log('project_updated', 'project', project.id,
                 user_id=current_user.id, ip_address=request.remote_addr)
    return jsonify(project_dict(project, detail=True))


@api_bp.route('/admin/projects/<int:pid>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_project_toggle(pid):
    """Soft delete — deactivate/reactivate a project."""
    project = _get_or_404(Project, pid)
    project.is_active = not project.is_active
    db.session.commit()
    AuditLog.log('project_updated', 'project', project.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'action': 'activated' if project.is_active else 'deactivated'})
    return jsonify(project_dict(project))


@api_bp.route('/admin/projects/<int:pid>', methods=['DELETE'])
@login_required
@admin_required
def admin_project_delete(pid):
    """Hard delete — removes the project and everything under it."""
    project = _get_or_404(Project, pid)

    # Refuse while anything is scheduled: deleting the rows would leave live
    # APScheduler jobs pointing at requests that no longer exist.
    active = EnvironmentRequest.query.join(Environment).filter(
        Environment.project_id == pid,
        EnvironmentRequest.status.in_(['pending', 'approved', 'active', 'starting']),
    ).count()
    if active:
        return jsonify({'error': f'Cannot delete a project with {active} open '
                                 f'request(s). Cancel or complete them first.'}), 400

    name = project.name
    db.session.delete(project)
    db.session.commit()
    AuditLog.log('project_deleted', 'project', pid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'name': name})
    return jsonify({'deleted': True, 'id': pid})


# ---------------------------------------------------------------------------
# Environments (nested under a project)
# ---------------------------------------------------------------------------

@api_bp.route('/admin/projects/<int:pid>/environments', methods=['POST'])
@login_required
@admin_required
def admin_environment_create(pid):
    project = _get_or_404(Project, pid)
    data = request.get_json(silent=True) or {}

    name = strip(data.get('name')).lower()
    display_name = strip(data.get('display_name'))
    if not name:
        return jsonify({'error': 'Environment name is required.'}), 400
    if not display_name:
        return jsonify({'error': 'Display name is required.'}), 400
    if project.environments.filter_by(name=name).first():
        return jsonify({'error': f'Environment "{name}" already exists in this project.'}), 400

    env = Environment(
        project_id=project.id,
        name=name,
        display_name=display_name,
        resource_group=data.get('resource_group'),
        region=data.get('region'),
        description=data.get('description'),
    )
    db.session.add(env)
    db.session.commit()

    AuditLog.log('environment_created', 'environment', env.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'name': env.name, 'project': project.name})
    return jsonify(environment_dict(env)), 201


@api_bp.route('/admin/projects/<int:pid>/environments/<int:eid>', methods=['PUT'])
@login_required
@admin_required
def admin_environment_edit(pid, eid):
    _get_or_404(Project, pid)
    env = _get_or_404(Environment, eid)
    data = request.get_json(silent=True) or {}

    name = strip(data.get('name')).lower()
    display_name = strip(data.get('display_name'))
    if not name:
        return jsonify({'error': 'Environment name is required.'}), 400
    if not display_name:
        return jsonify({'error': 'Display name is required.'}), 400

    clash = env.project.environments.filter_by(name=name).first()
    if clash and clash.id != env.id:
        return jsonify({'error': f'Environment "{name}" already exists in this project.'}), 400

    env.name = name
    env.display_name = display_name
    env.resource_group = data.get('resource_group')
    env.region = data.get('region')
    env.description = data.get('description')
    db.session.commit()

    AuditLog.log('environment_updated', 'environment', env.id,
                 user_id=current_user.id, ip_address=request.remote_addr)
    return jsonify(environment_dict(env))


@api_bp.route('/admin/projects/<int:pid>/environments/<int:eid>', methods=['DELETE'])
@login_required
@admin_required
def admin_environment_delete(pid, eid):
    _get_or_404(Project, pid)
    env = _get_or_404(Environment, eid)
    name = env.display_name
    db.session.delete(env)
    db.session.commit()
    AuditLog.log('environment_deleted', 'environment', eid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'name': name})
    return jsonify({'deleted': True, 'id': eid})


# ---------------------------------------------------------------------------
# Cloud services (nested under an environment)
# ---------------------------------------------------------------------------

def _service_body(data, project):
    """Validate a service create/edit body. Returns (values, error)."""
    name = strip(data.get('name'))
    service_type = strip(data.get('service_type'))
    cloud_resource_id = strip(data.get('cloud_resource_id'))

    if not name:
        return None, 'Service name is required.'
    if not cloud_resource_id:
        return None, 'Cloud Resource ID is required.'
    if service_type not in CloudService.SERVICE_TYPES.get(project.cloud_provider, []):
        return None, f'Invalid service type for {project.cloud_provider}.'
    try:
        hourly_cost = float(data.get('hourly_cost') or 0.0)
    except (TypeError, ValueError):
        return None, 'hourly_cost must be a number.'
    if hourly_cost < 0:
        return None, 'hourly_cost cannot be negative.'

    return (name, service_type, cloud_resource_id, hourly_cost,
            strip(data.get('region'))), None


@api_bp.route('/admin/environments/<int:eid>/services', methods=['GET'])
@login_required
@admin_required
def admin_environment_services(eid):
    env = _get_or_404(Environment, eid)
    services = env.services.order_by(CloudService.name).all()
    return jsonify({
        'services': [service_dict(s) for s in services],
        'service_types': [
            {'value': t, 'label': CloudService.SERVICE_TYPE_LABELS.get(t, t)}
            for t in CloudService.SERVICE_TYPES.get(env.project.cloud_provider, [])
        ],
    })


@api_bp.route('/admin/environments/<int:eid>/services', methods=['POST'])
@login_required
@admin_required
def admin_service_create(eid):
    env = _get_or_404(Environment, eid)
    values, error = _service_body(request.get_json(silent=True) or {}, env.project)
    if error:
        return jsonify({'error': error}), 400
    name, service_type, resource_id, hourly_cost, region = values

    service = CloudService(
        environment_id=env.id,
        name=name,
        service_type=service_type,
        cloud_resource_id=resource_id,
        hourly_cost=hourly_cost,
        # Captured at discovery so start/stop/status target the resource's own
        # region, not just the project default.
        cloud_config={'region': region} if region else None,
    )
    db.session.add(service)
    db.session.commit()

    AuditLog.log('service_created', 'cloud_service', service.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'name': service.name, 'type': service.service_type})
    return jsonify(service_dict(service)), 201


@api_bp.route('/admin/services/<int:sid>', methods=['PUT'])
@login_required
@admin_required
def admin_service_edit(sid):
    service = _get_or_404(CloudService, sid)
    values, error = _service_body(request.get_json(silent=True) or {},
                                  service.environment.project)
    if error:
        return jsonify({'error': error}), 400
    name, service_type, resource_id, hourly_cost, region = values

    service.name = name
    service.service_type = service_type
    service.cloud_resource_id = resource_id
    service.hourly_cost = hourly_cost
    if region:
        service.cloud_config = {**(service.cloud_config or {}), 'region': region}
    db.session.commit()

    AuditLog.log('service_updated', 'cloud_service', service.id,
                 user_id=current_user.id, ip_address=request.remote_addr)
    return jsonify(service_dict(service))


@api_bp.route('/admin/services/<int:sid>', methods=['DELETE'])
@login_required
@admin_required
def admin_service_delete(sid):
    service = _get_or_404(CloudService, sid)
    name = service.name
    db.session.delete(service)
    db.session.commit()
    AuditLog.log('service_deleted', 'cloud_service', sid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'name': name})
    return jsonify({'deleted': True, 'id': sid})


@api_bp.route('/admin/services/<int:sid>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_service_toggle(sid):
    service = _get_or_404(CloudService, sid)
    service.is_active = not service.is_active
    db.session.commit()
    return jsonify(service_dict(service))


# ---------------------------------------------------------------------------
# Project members
# ---------------------------------------------------------------------------

@api_bp.route('/admin/projects/<int:pid>/members', methods=['GET'])
@login_required
@admin_required
def admin_project_members(pid):
    project = _get_or_404(Project, pid)
    member_ids = {m.user_id for m in project.members.all()}
    available = User.query.filter_by(is_active=True).order_by(User.username).all()
    return jsonify({
        'members': [member_dict(m) for m in project.members.all()],
        'available_users': [user_dict(u) for u in available if u.id not in member_ids],
    })


@api_bp.route('/admin/projects/<int:pid>/members', methods=['POST'])
@login_required
@admin_required
def admin_member_add(pid):
    project = _get_or_404(Project, pid)
    data = request.get_json(silent=True) or {}

    # Accept either identifier: the UI picks a username, scripts tend to have an id.
    user_id = data.get('user_id')
    username = strip(data.get('username'))
    if user_id:
        user = db.session.get(User, user_id)
    elif username:
        user = User.query.filter_by(username=username).first()
    else:
        return jsonify({'error': 'A username or user_id is required.'}), 400

    if user is None:
        return jsonify({'error': 'Unknown user.'}), 400
    if project.members.filter_by(user_id=user.id).first():
        return jsonify({'error': 'That user is already a member.'}), 400

    project_role = (data.get('project_role') or 'developer').strip().lower()
    if project_role not in ProjectMember.ROLES:
        return jsonify({'error': f'Role must be one of: {", ".join(ProjectMember.ROLES)}.'}), 400

    member = ProjectMember(project_id=project.id, user_id=user.id,
                           added_by=current_user.id,
                           project_role=project_role,
                           can_view_secrets=bool(data.get('can_view_secrets', False)))
    db.session.add(member)
    db.session.commit()

    AuditLog.log('member_added', 'project', project.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'username': user.username, 'project_role': project_role})
    return jsonify(member_dict(member)), 201


@api_bp.route('/admin/projects/<int:pid>/members/<int:mid>', methods=['PUT'])
@login_required
@admin_required
def admin_member_update(pid, mid):
    """Update this member's project role and/or their permission to reveal secrets."""
    _get_or_404(Project, pid)
    member = _get_or_404(ProjectMember, mid)
    if member.project_id != pid:
        return jsonify({'error': 'Not found.'}), 404

    data = request.get_json(silent=True) or {}
    if 'can_view_secrets' not in data and 'project_role' not in data:
        return jsonify({'error': 'Nothing to update.'}), 400

    if 'project_role' in data:
        project_role = (data.get('project_role') or '').strip().lower()
        if project_role not in ProjectMember.ROLES:
            return jsonify({'error': f'Role must be one of: {", ".join(ProjectMember.ROLES)}.'}), 400
        member.project_role = project_role

    if 'can_view_secrets' in data:
        member.can_view_secrets = bool(data['can_view_secrets'])

    db.session.commit()

    AuditLog.log('member_permission_updated', 'project', pid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'username': member.user.username,
                          'can_view_secrets': member.can_view_secrets,
                          'project_role': member.project_role})
    return jsonify(member_dict(member))


@api_bp.route('/admin/projects/<int:pid>/members/<int:mid>', methods=['DELETE'])
@login_required
@admin_required
def admin_member_remove(pid, mid):
    _get_or_404(Project, pid)
    member = _get_or_404(ProjectMember, mid)
    username = member.user.username
    db.session.delete(member)
    db.session.commit()
    AuditLog.log('member_removed', 'project', pid,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'username': username})
    return jsonify({'deleted': True, 'id': mid})


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@api_bp.route('/admin/users', methods=['GET'])
@login_required
@admin_required
def admin_users():
    users = User.query.order_by(User.username).all()
    return jsonify({'users': [user_dict(u) for u in users], 'roles': ROLES})


@api_bp.route('/admin/users', methods=['POST'])
@login_required
@admin_required
def admin_user_create():
    data = request.get_json(silent=True) or {}
    username = strip(data.get('username'))
    email = strip(data.get('email')).lower()
    password = data.get('password') or ''
    role_name = data.get('role')

    if not username or not email:
        return jsonify({'error': 'Username and email are required.'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400

    role = Role.query.filter_by(name=role_name).first()
    if role is None:
        return jsonify({'error': 'Invalid role.'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'That username is taken.'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'That email is already registered.'}), 400

    user = User(username=username, email=email, role_id=role.id)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    AuditLog.log('user_created', 'user', user.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'username': username, 'role': role.name})
    return jsonify(user_dict(user)), 201


@api_bp.route('/admin/users/<int:uid>', methods=['PUT'])
@login_required
@admin_required
def admin_user_edit(uid):
    user = _get_or_404(User, uid)
    data = request.get_json(silent=True) or {}

    email = strip(data.get('email')).lower()
    if email and email != user.email:
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'That email is already registered.'}), 400
        user.email = email

    if data.get('role'):
        role = Role.query.filter_by(name=data['role']).first()
        if role is None:
            return jsonify({'error': 'Invalid role.'}), 400
        # Guard against an admin demoting themselves out of the last admin seat
        # and locking everyone out of project administration.
        if user.id == current_user.id and role.name != 'admin':
            admins = User.query.join(Role).filter(Role.name == 'admin',
                                                  User.is_active.is_(True)).count()
            if admins <= 1:
                return jsonify({'error': 'You are the only admin — promote someone '
                                         'else before changing your own role.'}), 400
        user.role_id = role.id

    if 'is_active' in data:
        if user.id == current_user.id and not data['is_active']:
            return jsonify({'error': 'You cannot deactivate your own account.'}), 400
        user.is_active = bool(data['is_active'])

    password = data.get('password')
    if password:
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters.'}), 400
        user.set_password(password)

    db.session.commit()
    AuditLog.log('user_updated', 'user', user.id,
                 user_id=current_user.id, ip_address=request.remote_addr)
    return jsonify(user_dict(user))
