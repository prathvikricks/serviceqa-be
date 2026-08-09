"""JSON dashboard endpoints — mirrors blueprints/dashboard/routes.py."""
from datetime import datetime, timedelta, timezone

from flask import jsonify, request
from flask_login import login_required, current_user

from ...extensions import db
from ...models.request import EnvironmentRequest, ScheduledJob
from ...models.environment import Environment
from ...models.budget import CostRecord
from ...decorators import devops_required
from . import api_bp
from .serializers import request_dict, scheduled_job_dict


def _scoped(query):
    """Developers only see their own requests; devops/admin see everything."""
    if current_user.is_developer:
        return query.filter_by(requester_id=current_user.id)
    return query


@api_bp.route('/dashboard')
@login_required
def dashboard():
    projects = current_user.get_projects()

    active = _scoped(
        EnvironmentRequest.query.filter(
            EnvironmentRequest.status.in_(['active', 'starting'])
        )
    ).order_by(EnvironmentRequest.start_time).all()

    pending = _scoped(
        EnvironmentRequest.query.filter_by(status='pending')
    ).order_by(EnvironmentRequest.created_at.desc()).all()

    recent = _scoped(
        EnvironmentRequest.query.filter(
            EnvironmentRequest.status.in_(['completed', 'declined', 'failed'])
        )
    ).order_by(EnvironmentRequest.updated_at.desc()).limit(10).all()

    total_environments = sum(
        p.environments.filter_by(is_active=True).count() for p in projects
    )

    return jsonify({
        'stats': {
            'active_requests': len(active),
            'pending_approvals': len(pending),
            'total_projects': len(projects),
            'total_environments': total_environments,
        },
        'active_requests': [request_dict(r) for r in active],
        'pending_approvals': [request_dict(r) for r in pending],
        'recent_requests': [request_dict(r) for r in recent],
    })


@api_bp.route('/dashboard/analytics')
@login_required
def analytics():
    """Aggregated metrics for the graphical dashboard (scoped to the user)."""
    ER = EnvironmentRequest
    now = datetime.now(timezone.utc)
    since_30 = now - timedelta(days=30)

    def scoped():
        return _scoped(ER.query)

    total_requests = scoped().count()
    active = scoped().filter(ER.status.in_(['active', 'starting'])).count()
    pending = scoped().filter_by(status='pending').count()
    completed_30d = scoped().filter(
        ER.status == 'completed', ER.updated_at >= since_30).count()

    est_cost_30d = _scoped(db.session.query(
        db.func.coalesce(db.func.sum(ER.estimated_cost), 0.0))
        .filter(ER.created_at >= since_30)).scalar() or 0.0

    projects = current_user.get_projects()
    total_environments = sum(
        p.environments.filter_by(is_active=True).count() for p in projects)

    # Requests per day, last 30 days (fill gaps with 0 — built in Python).
    day_col = db.func.date(ER.created_at)
    rows = _scoped(db.session.query(day_col, db.func.count(ER.id))
                   .filter(ER.created_at >= since_30)
                   .group_by(day_col)).all()
    counts = {str(d): int(c) for d, c in rows}
    requests_over_time = []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        requests_over_time.append({'day': day[5:], 'count': counts.get(day, 0)})

    by_status = [{'status': s, 'count': int(c)} for s, c in
                 _scoped(db.session.query(ER.status, db.func.count(ER.id))
                         .group_by(ER.status)).all()]
    by_action = [{'action_type': a or 'start_stop', 'count': int(c)} for a, c in
                 _scoped(db.session.query(ER.action_type, db.func.count(ER.id))
                         .group_by(ER.action_type)).all()]

    # Cost by month — prefer recorded costs; fall back to estimated by month.
    cost_rows = (db.session.query(CostRecord.month, db.func.sum(CostRecord.cost))
                 .group_by(CostRecord.month).order_by(CostRecord.month.desc())
                 .limit(6).all())
    if cost_rows and any((c or 0) > 0 for _, c in cost_rows):
        cost_by_month = [{'month': m, 'cost': round(float(c or 0), 2)}
                         for m, c in reversed(cost_rows)]
    else:
        month_col = db.func.substr(db.func.cast(ER.created_at, db.Text), 1, 7)
        # Tenant filter (in _scoped) must be applied before limit() — SQLAlchemy
        # forbids filter() after limit()/offset().
        est_rows = (_scoped(db.session.query(
            month_col, db.func.coalesce(db.func.sum(ER.estimated_cost), 0.0)))
            .group_by(month_col).order_by(month_col.desc()).limit(6).all())
        cost_by_month = [{'month': m, 'cost': round(float(c or 0), 2)}
                         for m, c in reversed(est_rows)]

    top_environments = [{'name': name, 'count': int(c)} for name, c in
                        _scoped(db.session.query(
                            Environment.display_name, db.func.count(ER.id))
                            .join(Environment, Environment.id == ER.environment_id))
                        .group_by(Environment.display_name)
                        .order_by(db.func.count(ER.id).desc()).limit(6).all()]

    return jsonify({
        'kpis': {
            'total_requests': total_requests,
            'active': active,
            'pending': pending,
            'completed_30d': completed_30d,
            'est_cost_30d': round(float(est_cost_30d), 2),
            'projects': len(projects),
            'environments': total_environments,
        },
        'requests_over_time': requests_over_time,
        'by_status': by_status,
        'by_action': by_action,
        'cost_by_month': cost_by_month,
        'top_environments': top_environments,
    })


@api_bp.route('/dashboard/activity')
@login_required
@devops_required
def activity():
    status_filter = request.args.get('status', '')

    jobs_query = ScheduledJob.query.join(EnvironmentRequest)
    if status_filter:
        jobs_query = jobs_query.filter(ScheduledJob.status == status_filter)
    jobs = jobs_query.order_by(ScheduledJob.scheduled_time.desc()).limit(100).all()

    in_flight = EnvironmentRequest.query.filter(
        EnvironmentRequest.status.in_(['starting', 'stopping', 'approved'])
    ).order_by(EnvironmentRequest.updated_at.desc()).all()

    return jsonify({
        'jobs': [scheduled_job_dict(j) for j in jobs],
        'in_flight': [request_dict(r) for r in in_flight],
        'job_statuses': ['pending', 'running', 'completed', 'failed'],
    })
