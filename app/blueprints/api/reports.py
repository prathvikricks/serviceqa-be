"""Admin read-only reports: form metadata, the audit log, and cost breakdowns."""
from flask import jsonify, request
from flask_login import login_required

from ...extensions import db
from ...decorators import admin_required
from ...models.audit import AuditLog
from ...models.budget import CostRecord
from ...models.environment import CloudService, Environment
from ...models.project import Project
from . import api_bp
from .admin import CLOUD_PROVIDERS, MODES, ROLES
from .helpers import _get_or_404
from .serializers import audit_log_dict


@api_bp.route('/admin/meta')
@login_required
@admin_required
def admin_meta():
    """Everything the admin forms need to render their dropdowns."""
    return jsonify({
        'cloud_providers': CLOUD_PROVIDERS,
        'modes': MODES,
        'roles': ROLES,
        'service_types': CloudService.SERVICE_TYPES,
        'service_type_labels': CloudService.SERVICE_TYPE_LABELS,
    })


@api_bp.route('/admin/audit')
@login_required
@admin_required
def admin_audit():
    """Paginated audit trail, filterable by action and entity type."""
    query = AuditLog.query
    action = request.args.get('action', '').strip()
    entity_type = request.args.get('entity_type', '').strip()
    if action:
        query = query.filter_by(action=action)
    if entity_type:
        query = query.filter_by(entity_type=entity_type)

    pagination = query.order_by(AuditLog.created_at.desc()).paginate(
        page=request.args.get('page', 1, type=int),
        per_page=request.args.get('per_page', 50, type=int),
        error_out=False,
    )

    return jsonify({
        'entries': [audit_log_dict(e) for e in pagination.items],
        'page': pagination.page,
        'pages': pagination.pages,
        'total': pagination.total,
        'actions': AuditLog.ACTIONS,
    })


@api_bp.route('/admin/projects/<int:pid>/costs')
@login_required
@admin_required
def admin_project_costs(pid):
    """Recorded runtime cost for one project, for a given month ('YYYY-MM').

    These are *actual* costs written when a request's cycle completes, not the
    estimate shown at request time — the two diverge whenever a window is
    extended, cancelled early, or a service fails to start.
    """
    project = _get_or_404(Project, pid)
    month = request.args.get('month', '').strip()
    if not month:
        latest = (db.session.query(db.func.max(CostRecord.month))
                  .filter(CostRecord.project_id == pid).scalar())
        month = latest or ''

    records = (CostRecord.query
               .filter(CostRecord.project_id == pid, CostRecord.month == month)
               .order_by(CostRecord.recorded_at.desc()).all()) if month else []

    # Per-environment totals, including environments with no spend this month so
    # the report shows a complete picture rather than silently omitting them.
    totals = {}
    for rec in records:
        totals[rec.environment_id] = totals.get(rec.environment_id, 0.0) + (rec.cost or 0.0)

    environments = [{
        'environment_id': env.id,
        'environment': env.display_name,
        'cost': round(totals.get(env.id, 0.0), 2),
        'hourly_cost': env.total_hourly_cost,
    } for env in project.environments.order_by(Environment.name).all()]

    months = [m for (m,) in db.session.query(CostRecord.month)
              .filter(CostRecord.project_id == pid)
              .distinct().order_by(CostRecord.month.desc()).all()]

    return jsonify({
        'project': {'id': project.id, 'name': project.name},
        'month': month,
        'available_months': months,
        'environments': environments,
        'records': [{
            'id': r.id,
            'request_id': r.request_id,
            'environment_id': r.environment_id,
            'environment': r.environment.display_name if r.environment else None,
            'runtime_hours': r.runtime_hours,
            'cost': r.cost,
            'month': r.month,
            'recorded_at': r.recorded_at.isoformat() if r.recorded_at else None,
        } for r in records],
        'total_cost': round(sum(r.cost or 0.0 for r in records), 2),
    })
