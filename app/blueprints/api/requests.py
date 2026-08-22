"""JSON request endpoints — mirrors blueprints/requests/routes.py."""
import logging
import re
from datetime import datetime, date
from flask import jsonify, request
from flask_login import login_required, current_user

from ...extensions import db
from ...models.request import EnvironmentRequest, RequestService
from ...models.environment import Environment
from ...models.audit import AuditLog
from ...services.cost_estimator import estimate_request_cost
from . import api_bp
from .helpers import _get_or_404
from .serializers import request_dict, approval_dict, audit_log_dict

logger = logging.getLogger(__name__)


def _scoped(query):
    """Developers only see their own requests; devops/admin see everything."""
    if current_user.is_developer:
        return query.filter_by(requester_id=current_user.id)
    return query


def _parse_dt(value):
    """Parse an ISO datetime string; returns None on bad/missing input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


_HM_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def _valid_hm(value):
    return bool(value and _HM_RE.match(value))


@api_bp.route('/requests')
@login_required
def list_requests():
    status_filter = request.args.get('status', '')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    query = _scoped(EnvironmentRequest.query)
    if status_filter:
        query = query.filter_by(status=status_filter)

    pagination = query.order_by(EnvironmentRequest.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False)

    return jsonify({
        'requests': [request_dict(r) for r in pagination.items],
        'statuses': EnvironmentRequest.STATUSES,
        'page': pagination.page,
        'pages': pagination.pages,
        'total': pagination.total,
    })


_REPO_NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,120}$')


def _linked_conversation_id(data, project_id):
    """The chat conversation to record on this request, if it is legitimately ours.

    Silently ignored rather than rejected when it does not check out: the link
    is provenance, not authorization, and a stale id from a reloaded tab should
    not block a valid request.
    """
    from ...models.chat import ChatConversation

    conversation_id = data.get('conversation_id')
    if not conversation_id:
        return None
    convo = db.session.get(ChatConversation, conversation_id)
    if convo is None or convo.user_id != current_user.id:
        return None
    if project_id is not None and convo.project_id != project_id:
        return None
    return convo.id


@api_bp.route('/git/providers')
@login_required
def git_providers():
    """Which Git providers the backend can create repos on (token configured)."""
    from ...services.git_manager import configured_providers
    return jsonify({'providers': configured_providers()})


def _create_repo_request(data):
    """Create a pending 'repo' request. The provider is chosen by the approver."""
    from ...models.project import Project

    repo_name = (data.get('repo_name') or '').strip()
    description = (data.get('repo_description') or '').strip()
    visibility = (data.get('repo_visibility') or 'private').strip().lower()
    project_id = data.get('project_id')
    reason = (data.get('reason') or '').strip()

    if not _REPO_NAME_RE.match(repo_name):
        return jsonify({'error': 'Repository name may only contain letters, '
                                 'numbers, dot, dash and underscore.'}), 400
    if visibility not in ('private', 'public'):
        return jsonify({'error': 'Visibility must be private or public.'}), 400

    project = None
    if project_id:
        project = db.session.get(Project, project_id)
        if not project:
            return jsonify({'error': 'Linked project not found.'}), 400

    # reason is NOT NULL on the shared table — fall back to the description
    # or the repo name so the repo form doesn't have to duplicate the field.
    reason = reason or description or f'Create repository {repo_name}'

    repo_request = EnvironmentRequest(
        requester_id=current_user.id,
        request_type='repo',
        action_type='create_repo',
        status='pending',
        reason=reason,
        project_id=project.id if project else None,
        repo_name=repo_name,
        repo_description=description or None,
        repo_visibility=visibility,
        conversation_id=_linked_conversation_id(data, project.id if project else None),
    )
    db.session.add(repo_request)
    db.session.commit()

    AuditLog.log('request_created', 'request', repo_request.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={
                     'type': 'repo',
                     'repo_name': repo_name,
                     'visibility': visibility,
                     'project': project.name if project else None,
                 })

    return jsonify(request_dict(repo_request, detail=True)), 201


@api_bp.route('/requests', methods=['POST'])
@login_required
def create_request():
    data = request.get_json(silent=True) or {}

    if (data.get('request_type') or 'service') == 'repo':
        return _create_repo_request(data)

    environment_id = data.get('environment_id')
    action_type = data.get('action_type', 'start_stop')
    schedule_type = data.get('schedule_type', 'once')
    reason = data.get('reason')

    if not environment_id:
        return jsonify({'error': 'environment_id is required'}), 400
    if not reason:
        return jsonify({'error': 'reason is required'}), 400
    if schedule_type not in ('once', 'weekly'):
        return jsonify({'error': 'Invalid schedule_type'}), 400

    # Recurrence fields (weekly only).
    recurrence_days = None
    start_hm = stop_hm = None
    recur_until = None

    if schedule_type == 'weekly':
        from ...services.scheduler_service import next_occurrence
        days_in = set(data.get('recurrence_days') or [])
        days = [d for d in EnvironmentRequest.WEEKDAYS if d in days_in]
        start_hm = (data.get('start_hm') or '').strip()
        stop_hm = (data.get('stop_hm') or '').strip()
        if not days:
            return jsonify({'error': 'Select at least one weekday.'}), 400
        if not _valid_hm(start_hm) or not _valid_hm(stop_hm):
            return jsonify({'error': 'Start and stop times must be valid HH:MM.'}), 400
        if start_hm >= stop_hm:
            return jsonify({'error': 'Stop time must be after start time.'}), 400
        raw_until = data.get('recur_until')
        if raw_until:
            try:
                recur_until = date.fromisoformat(raw_until)
            except (ValueError, TypeError):
                return jsonify({'error': 'Invalid end date.'}), 400
        # Populate start_time/end_time with the next upcoming occurrence.
        start_time = next_occurrence(days, start_hm)
        if start_time is None:
            return jsonify({'error': 'Could not compute a schedule from those days/times.'}), 400
        sh, sm = (int(x) for x in stop_hm.split(':'))
        end_time = start_time.replace(hour=sh, minute=sm, second=0, microsecond=0)
        recurrence_days = ','.join(days)
    else:
        start_time = _parse_dt(data.get('start_time'))
        end_time = _parse_dt(data.get('end_time'))
        if start_time is None:
            return jsonify({'error': 'Invalid or missing start_time'}), 400
        if end_time is None:
            return jsonify({'error': 'Invalid or missing end_time'}), 400

    env = db.session.get(Environment, environment_id)
    if not env or not current_user.is_member_of(env.project_id):
        return jsonify({'error': 'Invalid environment or access denied'}), 403

    # For weekly, this is the per-occurrence (one-window) cost.
    estimated_cost = estimate_request_cost(env, start_time, end_time)

    env_request = EnvironmentRequest(
        requester_id=current_user.id,
        environment_id=env.id,
        action_type=action_type,
        schedule_type=schedule_type,
        recurrence_days=recurrence_days,
        start_hm=start_hm,
        stop_hm=stop_hm,
        recur_until=recur_until,
        start_time=start_time,
        end_time=end_time,
        reason=reason,
        status='pending',
        estimated_cost=estimated_cost,
        conversation_id=_linked_conversation_id(data, env.project_id),
    )
    db.session.add(env_request)
    db.session.flush()

    # Add all active services for this environment
    active_services = env.services.filter_by(is_active=True).all()
    for svc in active_services:
        db.session.add(RequestService(
            request_id=env_request.id,
            cloud_service_id=svc.id,
        ))

    db.session.commit()

    AuditLog.log('request_created', 'request', env_request.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={
                     'project': env.project.name,
                     'environment': env.display_name,
                     'start': str(env_request.start_time),
                     'end': str(env_request.end_time),
                     'estimated_cost': estimated_cost,
                 })

    return jsonify(request_dict(env_request, detail=True)), 201


@api_bp.route('/requests/<int:request_id>')
@login_required
def request_detail(request_id):
    env_request = _get_or_404(EnvironmentRequest, request_id)

    # Access check (mirrors HTML route). Repo requests have no environment, so
    # fall back to their linked project (if any).
    if current_user.is_developer and env_request.requester_id != current_user.id:
        proj = env_request.project
        if not proj or not current_user.is_member_of(proj.id):
            return jsonify({'error': 'Access denied'}), 403

    data = request_dict(env_request, detail=True)
    data['approval'] = approval_dict(env_request.approval)

    # Activity timeline — audit entries for this request (mirrors detail.html).
    activity = AuditLog.query.filter_by(
        entity_type='request', entity_id=request_id,
    ).order_by(AuditLog.created_at.desc()).limit(25).all()
    data['activity'] = [audit_log_dict(a) for a in activity]
    return jsonify(data)


@api_bp.route('/requests/<int:request_id>/cancel', methods=['POST'])
@login_required
def cancel_request(request_id):
    env_request = _get_or_404(EnvironmentRequest, request_id)

    if env_request.requester_id != current_user.id and not current_user.is_devops:
        return jsonify({'error': 'Access denied'}), 403

    if env_request.status not in ('pending', 'approved'):
        return jsonify({'error': 'Cannot cancel this request'}), 400

    env_request.status = 'cancelled'
    db.session.commit()

    AuditLog.log('request_cancelled', 'request', request_id,
                 user_id=current_user.id, ip_address=request.remote_addr)

    return jsonify(request_dict(env_request, detail=True))


@api_bp.route('/requests/<int:request_id>/extend', methods=['POST'])
@login_required
def extend_request(request_id):
    parent = _get_or_404(EnvironmentRequest, request_id)

    if parent.status != 'active':
        return jsonify({'error': 'Can only extend active requests'}), 400

    if parent.requester_id != current_user.id and not current_user.is_devops:
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    new_end_time = _parse_dt(data.get('new_end_time'))
    reason = data.get('reason')

    if new_end_time is None:
        return jsonify({'error': 'Invalid or missing new_end_time'}), 400
    if new_end_time <= parent.end_time:
        return jsonify({'error': 'New end time must be after current end time'}), 400

    extension = EnvironmentRequest(
        requester_id=current_user.id,
        environment_id=parent.environment_id,
        start_time=parent.end_time,
        end_time=new_end_time,
        reason=f'Extension: {reason}',
        status='pending',
        parent_request_id=parent.id,
    )
    db.session.add(extension)
    db.session.commit()

    AuditLog.log('request_created', 'request', extension.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'type': 'extension', 'parent_id': parent.id})

    return jsonify(request_dict(extension, detail=True)), 201
