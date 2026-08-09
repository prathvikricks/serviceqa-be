"""JSON approval endpoints — mirrors blueprints/approvals/routes.py."""
import logging
from datetime import datetime, timezone
from flask import jsonify, request, current_app
from flask_login import login_required, current_user

from ...extensions import db
from ...decorators import devops_required
from ...models.request import EnvironmentRequest
from ...models.approval import Approval
from ...models.environment import Environment
from ...models.audit import AuditLog
from . import api_bp
from .helpers import _get_or_404
from .serializers import request_dict

logger = logging.getLogger(__name__)


@api_bp.route('/approvals')
@login_required
@devops_required
def approvals_list():
    status = request.args.get('status', 'pending')
    query = EnvironmentRequest.query

    if status == 'pending':
        query = query.filter_by(status='pending')
    elif status == 'all':
        pass
    else:
        query = query.filter_by(status=status)

    requests_list = query.order_by(EnvironmentRequest.created_at.desc()).all()

    return jsonify({
        'requests': [request_dict(r) for r in requests_list],
        'statuses': EnvironmentRequest.STATUSES,
    })


@api_bp.route('/approvals/<int:request_id>/approve', methods=['POST'])
@login_required
@devops_required
def approve(request_id):
    env_request = _get_or_404(EnvironmentRequest, request_id)

    if env_request.status != 'pending':
        return jsonify({'error': 'This request is no longer pending'}), 400

    data = request.get_json(silent=True) or {}
    comment = (data.get('comment') or '').strip()

    approval = Approval(
        request_id=request_id,
        approver_id=current_user.id,
        decision='approved',
        comment=comment if comment else None,
    )
    db.session.add(approval)
    env_request.status = 'approved'
    db.session.commit()

    AuditLog.log('request_approved', 'request', request_id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'comment': comment})

    # Schedule start/stop jobs
    try:
        from ...services.scheduler_service import schedule_request
        schedule_request(current_app._get_current_object(), env_request)
    except Exception as e:
        logger.error(f"Failed to schedule request: {e}")

    return jsonify(request_dict(env_request, detail=True))


@api_bp.route('/approvals/<int:request_id>/decline', methods=['POST'])
@login_required
@devops_required
def decline(request_id):
    env_request = _get_or_404(EnvironmentRequest, request_id)

    if env_request.status != 'pending':
        return jsonify({'error': 'This request is no longer pending'}), 400

    data = request.get_json(silent=True) or {}
    comment = (data.get('comment') or '').strip()

    approval = Approval(
        request_id=request_id,
        approver_id=current_user.id,
        decision='declined',
        comment=comment if comment else None,
    )
    db.session.add(approval)
    env_request.status = 'declined'
    db.session.commit()

    AuditLog.log('request_declined', 'request', request_id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'comment': comment})

    return jsonify(request_dict(env_request, detail=True))


@api_bp.route('/environments/<int:env_id>/emergency-stop', methods=['POST'])
@login_required
@devops_required
def emergency_stop(env_id):
    environment = _get_or_404(Environment, env_id)

    # Find all active requests for this environment
    active_requests = EnvironmentRequest.query.filter(
        EnvironmentRequest.environment_id == env_id,
        EnvironmentRequest.status.in_(['active', 'starting'])
    ).all()

    for req in active_requests:
        req.status = 'completed'
        for rs in req.services.all():
            rs.action_status = 'stopped'
            rs.stopped_at = datetime.now(timezone.utc)

    db.session.commit()

    AuditLog.log('emergency_stop', 'environment', env_id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={
                     'project': environment.project.name,
                     'environment': environment.display_name,
                     'affected_requests': [r.id for r in active_requests],
                 })

    # Stop all services via cloud manager
    try:
        from ...services.cloud_manager import CloudManagerFactory
        manager = CloudManagerFactory.get_manager(environment.project)
        for svc in environment.active_services:
            try:
                manager.stop_service(svc)
                svc.current_status = 'stopped'
            except Exception as e:
                logger.error(f"Emergency stop failed for {svc.name}: {e}")
        db.session.commit()
    except Exception as e:
        logger.error(f"Emergency stop cloud manager error: {e}")

    # Cancel scheduled jobs
    try:
        from ...services.scheduler_service import cancel_request_jobs
        for req in active_requests:
            cancel_request_jobs(req.id)
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'affected_requests': [request_dict(r) for r in active_requests],
    })
