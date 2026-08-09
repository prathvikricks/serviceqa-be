"""Read-only inventory + discovery endpoints.

These back the New Request form's project/environment pickers, the admin
resource picker (discovery against the cloud provider), and the polling the
request detail page does while a start/stop is in flight.
"""
import logging

from flask import jsonify, request
from flask_login import login_required, current_user

from ...models.project import Project
from ...models.environment import Environment, CloudService
from ...models.request import EnvironmentRequest, ScheduledJob
from ...decorators import devops_required
from . import api_bp
from .helpers import _get_or_404

logger = logging.getLogger(__name__)


def _require_access(project_id):
    """403 unless the current user can see this project."""
    if not current_user.is_member_of(project_id):
        return jsonify({'error': 'Access denied'}), 403
    return None


@api_bp.route('/projects')
@login_required
def my_projects():
    """Projects the current user can access — the New Request project picker."""
    return jsonify({
        'projects': [{
            'id': p.id,
            'name': p.name,
            'slug': p.slug,
            'cloud_provider': p.cloud_provider,
            'mode': p.mode,
            'environment_count': p.environments.filter_by(is_active=True).count(),
        } for p in current_user.get_projects()],
    })


@api_bp.route('/projects/<int:project_id>/environments')
@login_required
def project_environments(project_id):
    project = _get_or_404(Project, project_id)
    denied = _require_access(project_id)
    if denied:
        return denied

    return jsonify({'environments': [{
        'id': env.id,
        'name': env.name,
        'display_name': env.display_name,
        'total_hourly_cost': env.total_hourly_cost,
        'service_count': env.services.filter_by(is_active=True).count(),
        'services': [{
            'id': s.id,
            'name': s.name,
            'type': s.service_type,
            'status': s.current_status,
            'hourly_cost': s.hourly_cost,
        } for s in env.services.filter_by(is_active=True).all()],
    } for env in project.environments.filter_by(is_active=True).all()]})


@api_bp.route('/projects/<int:project_id>/service-types')
@login_required
@devops_required
def service_types(project_id):
    """Service types valid for this project's provider."""
    project = _get_or_404(Project, project_id)
    labels = CloudService.SERVICE_TYPE_LABELS
    return jsonify({
        'provider': project.cloud_provider,
        'mode': project.mode,
        'types': [{'value': t, 'label': labels.get(t, t)}
                  for t in CloudService.SERVICE_TYPES.get(project.cloud_provider, [])],
    })


@api_bp.route('/projects/<int:project_id>/discover/<service_type>')
@login_required
@devops_required
def discover_resources(project_id, service_type):
    """Ask the provider what resources of this type exist, so an admin picks
    from a real list instead of pasting an ARN or ARM id by hand.

    In mock mode this returns the simulated catalogue — same shape, no calls.
    """
    project = _get_or_404(Project, project_id)

    if service_type not in CloudService.SERVICE_TYPES.get(project.cloud_provider, []):
        return jsonify({'error': f'Invalid service type for {project.cloud_provider}'}), 400

    try:
        from ...services.cloud_manager import CloudManagerFactory
        manager = CloudManagerFactory.get_manager(project)
        resources = manager.list_resources(
            service_type,
            request.args.get('resource_group'),
            region=request.args.get('region'),
        )
        return jsonify({'resources': resources, 'count': len(resources)})
    except ValueError as e:
        # Missing/invalid credentials — the admin's problem to fix, not a 500.
        return jsonify({'error': str(e), 'resources': []}), 400
    except Exception as e:
        logger.error(f'Discovery failed for {project.name}/{service_type}: {e}')
        return jsonify({'error': f'Failed to reach {project.cloud_provider}: {e}',
                        'resources': []}), 502


@api_bp.route('/projects/<int:project_id>/resource-groups')
@login_required
@devops_required
def list_resource_groups(project_id):
    project = _get_or_404(Project, project_id)
    try:
        from ...services.cloud_manager import CloudManagerFactory
        manager = CloudManagerFactory.get_manager(project)
        return jsonify({'resource_groups': manager.list_resource_groups()})
    except Exception as e:
        logger.error(f'Failed to list resource groups for {project.name}: {e}')
        return jsonify({'error': str(e), 'resource_groups': []}), 502


@api_bp.route('/environments/<int:env_id>/status')
@login_required
def environment_status(env_id):
    env = _get_or_404(Environment, env_id)
    denied = _require_access(env.project_id)
    if denied:
        return denied

    active = EnvironmentRequest.query.filter(
        EnvironmentRequest.environment_id == env_id,
        EnvironmentRequest.status.in_(['active', 'starting']),
    ).all()

    return jsonify({
        'environment': env.display_name,
        'services': [{
            'id': s.id,
            'name': s.name,
            'type': s.service_type,
            'status': s.current_status,
            'last_check': s.last_status_check.isoformat() if s.last_status_check else None,
        } for s in env.services.filter_by(is_active=True).all()],
        'active_requests': [{
            'id': r.id,
            'requester': r.requester.username,
            'end_time': r.end_time.isoformat(),
        } for r in active],
    })


@api_bp.route('/requests/<int:request_id>/live')
@login_required
def request_live_status(request_id):
    """Live status for one request — polled by the detail page while a
    start/stop is running, so the per-service progress updates without a reload."""
    env_request = _get_or_404(EnvironmentRequest, request_id)

    if current_user.is_developer and env_request.requester_id != current_user.id:
        if not current_user.is_member_of(env_request.environment.project_id):
            return jsonify({'error': 'Access denied'}), 403

    return jsonify({
        'status': env_request.status,
        'updated_at': env_request.updated_at.isoformat() if env_request.updated_at else None,
        'services': [{
            'id': rs.id,
            'name': rs.cloud_service.name,
            'type': rs.cloud_service.service_type,
            'action_status': rs.action_status,
            'started_at': rs.started_at.isoformat() if rs.started_at else None,
            'stopped_at': rs.stopped_at.isoformat() if rs.stopped_at else None,
            'error': rs.error_message,
        } for rs in env_request.services.all()],
        'jobs': [{
            'id': j.id,
            'type': j.job_type,
            'scheduled_time': j.scheduled_time.isoformat(),
            'executed_at': j.executed_at.isoformat() if j.executed_at else None,
            'status': j.status,
            'error': j.error_message,
        } for j in env_request.scheduled_jobs.order_by(ScheduledJob.scheduled_time).all()],
    })
