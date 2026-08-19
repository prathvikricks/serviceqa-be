"""JSON serializers for API responses.

Lightweight `to_dict`-style helpers so the JSON API never leaks SQLAlchemy
objects or secret fields. Secrets (cloud credentials, SMTP/LLM keys) are always
masked — the React client only needs to know whether a value is set, not what it
is.
"""


def _dt(value):
    """Serialize a datetime (or None) to an ISO string."""
    return value.isoformat() if value else None


def user_dict(user):
    if user is None:
        return None
    return {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'role': user.role.name,
        'is_active': user.is_active,
        'is_admin': user.is_admin,
        'is_devops': user.is_devops,
        'is_developer': user.is_developer,
        'created_at': _dt(user.created_at),
    }


def project_dict(project, detail=False):
    data = {
        'id': project.id,
        'name': project.name,
        'slug': project.slug,
        'cloud_provider': project.cloud_provider,
        'mode': project.mode,
        'is_active': project.is_active,
        'environment_count': project.environments.filter_by(is_active=True).count(),
        'member_count': project.members.count(),
    }
    if detail:
        data['environments'] = [
            environment_dict(env) for env in project.environments.filter_by(is_active=True).all()
        ]
    return data


def environment_dict(env, with_services=False):
    data = {
        'id': env.id,
        'project_id': env.project_id,
        'name': env.name,
        'display_name': env.display_name,
        'region': env.region,
        'resource_group': env.resource_group,
        'total_hourly_cost': env.total_hourly_cost,
        'service_count': env.services.filter_by(is_active=True).count(),
    }
    if with_services:
        data['services'] = [
            service_dict(s) for s in env.services.filter_by(is_active=True).all()
        ]
    return data


def service_dict(svc):
    return {
        'id': svc.id,
        'environment_id': svc.environment_id,
        'name': svc.name,
        'service_type': svc.service_type,
        'cloud_resource_id': svc.cloud_resource_id,
        'hourly_cost': svc.hourly_cost,
        'current_status': svc.current_status,
        'is_active': svc.is_active,
        'last_status_check': _dt(svc.last_status_check),
    }


def request_dict(req, detail=False):
    env = req.environment
    project = req.project
    data = {
        'id': req.id,
        'requester': req.requester.username,
        'requester_id': req.requester_id,
        'request_type': req.request_type,
        'environment_id': req.environment_id,
        'environment': env.display_name if env else None,
        'project': project.name if project else None,
        'project_id': req.project_id,
        # Repo-request fields (null for service requests).
        'repo_name': req.repo_name,
        'repo_description': req.repo_description,
        'repo_visibility': req.repo_visibility,
        'git_provider': req.git_provider,
        'repo_url': req.repo_url,
        'git_error': req.git_error,
        'action_type': req.action_type,
        'start_time': _dt(req.start_time),
        'end_time': _dt(req.end_time),
        'schedule_type': req.schedule_type,
        'recurrence_days': req.recurrence_days_list,
        'start_hm': req.start_hm,
        'stop_hm': req.stop_hm,
        'recur_until': req.recur_until.isoformat() if req.recur_until else None,
        'recurrence_label': req.recurrence_label,
        'reason': req.reason,
        'status': req.status,
        'estimated_cost': req.estimated_cost,
        'duration_hours': req.duration_hours,
        'action_label': req.action_label,
        'parent_request_id': req.parent_request_id,
        'created_at': _dt(req.created_at),
        'updated_at': _dt(req.updated_at),
    }
    if detail:
        data['services'] = [request_service_dict(rs) for rs in req.services.all()]
        data['jobs'] = [scheduled_job_dict(j) for j in req.scheduled_jobs.all()]
    return data


def request_service_dict(rs):
    return {
        'id': rs.id,
        'name': rs.cloud_service.name,
        'type': rs.cloud_service.service_type,
        'action_status': rs.action_status,
        'started_at': _dt(rs.started_at),
        'stopped_at': _dt(rs.stopped_at),
        'error': rs.error_message,
    }


def scheduled_job_dict(job):
    return {
        'id': job.id,
        'request_id': job.request_id,
        'type': job.job_type,
        'scheduled_time': _dt(job.scheduled_time),
        'executed_at': _dt(job.executed_at),
        'status': job.status,
        'error': job.error_message,
    }


def approval_dict(approval):
    if approval is None:
        return None
    return {
        'id': approval.id,
        'request_id': approval.request_id,
        'approver': approval.approver.username if approval.approver else None,
        'decision': approval.decision,
        'comment': approval.comment,
        'auto_approved': approval.auto_approved,
        'decided_at': _dt(approval.decided_at),
    }


def audit_log_dict(entry):
    return {
        'id': entry.id,
        'action': entry.action,
        'entity_type': entry.entity_type,
        'entity_id': entry.entity_id,
        'user': entry.user.username if entry.user else None,
        'details': entry.details,
        'ip_address': entry.ip_address,
        'created_at': _dt(entry.created_at),
    }


def member_dict(member):
    return {
        'id': member.id,
        'project_id': member.project_id,
        'user_id': member.user_id,
        'username': member.user.username,
        'email': member.user.email,
        'role': member.user.role.name,
        'can_view_secrets': bool(member.can_view_secrets),
        'added_by': member.adder.username if member.adder else None,
        'added_at': _dt(member.added_at),
    }
