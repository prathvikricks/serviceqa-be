"""Vulnerability listing.

The listing half of the VulnWatch port: CVEs pulled from OSV.dev (see
services/vuln_scan) are triaged here. Global to devops + admins, like the ticket
queue — a vulnerability in Django affects every project that runs it, so scoping
the list per project would just hide it from the people who fix it.
"""
import csv
import io
from datetime import datetime, timezone

from flask import Response, current_app, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import or_

from ...decorators import devops_required
from ...extensions import db
from ...models.audit import AuditLog
from ...models.vulnerability import Vulnerability, VulnSourceStatus
from . import api_bp
from .helpers import _get_or_404
from .serializers import _utc, vulnerability_dict, vuln_source_status_dict


def _last_scan_at():
    """The most recent successful source scan, or None if never scanned."""
    row = (VulnSourceStatus.query
           .filter(VulnSourceStatus.last_scanned_at.isnot(None))
           .order_by(VulnSourceStatus.last_scanned_at.desc())
           .first())
    return row.last_scanned_at if row else None


@api_bp.route('/vulnerabilities/status')
@login_required
def vuln_status():
    """Capability probe, so the SPA nav can gate the page. No secrets leak here.

    `enabled` stays true once any finding exists: a list with history must not
    vanish because scanning was toggled off.
    """
    from ...services.vuln_scan import scan_enabled

    has_rows = db.session.query(Vulnerability.id).first() is not None
    last = _last_scan_at()
    return jsonify({
        'enabled': scan_enabled() or has_rows,
        'scan_enabled': scan_enabled(),
        'never_scanned': last is None,
        'last_scan_at': _utc(last),
        'severities': Vulnerability.SEVERITIES,
    })


@api_bp.route('/vulnerabilities')
@login_required
@devops_required
def vulnerabilities_list():
    query = Vulnerability.query

    severity = (request.args.get('severity') or '').strip()
    if severity and severity != 'all':
        query = query.filter_by(severity=severity)

    source = (request.args.get('source') or '').strip()
    if source and source != 'all':
        query = query.filter_by(source=source)

    acknowledged = (request.args.get('acknowledged') or '').strip().lower()
    if acknowledged == 'true':
        query = query.filter_by(acknowledged=True)
    elif acknowledged in ('false', 'open'):
        query = query.filter_by(acknowledged=False)
    # 'all' (or anything else) → no ack filter.

    term = (request.args.get('q') or '').strip()
    if term:
        like = f'%{term}%'
        query = query.filter(or_(Vulnerability.cve_id.ilike(like),
                                 Vulnerability.description.ilike(like)))

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    # Newest-first from the DB; the page is then re-sorted by severity so the
    # worst findings lead (SQLite has no natural order for the severity strings).
    pagination = query.order_by(Vulnerability.published.desc().nullslast(),
                                Vulnerability.first_seen.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    rows = sorted(pagination.items, key=lambda v: -v.severity_rank)

    counts = {s: Vulnerability.query.filter_by(severity=s).count()
              for s in Vulnerability.SEVERITIES}
    ack_counts = {
        'acknowledged': Vulnerability.query.filter_by(acknowledged=True).count(),
        'open': Vulnerability.query.filter_by(acknowledged=False).count(),
    }
    sources = [r[0] for r in db.session.query(Vulnerability.source).distinct().all()]

    return jsonify({
        'vulnerabilities': [vulnerability_dict(v) for v in rows],
        'severities': Vulnerability.SEVERITIES,
        'sources': sorted(sources),
        'counts': counts,
        'ack_counts': ack_counts,
        'page': pagination.page,
        'pages': pagination.pages,
        'total': pagination.total,
    })


@api_bp.route('/vulnerabilities/sources')
@login_required
@devops_required
def vuln_sources():
    """Per-source last-scan status for the panel."""
    rows = VulnSourceStatus.query.order_by(VulnSourceStatus.source).all()
    return jsonify({'sources': [vuln_source_status_dict(s) for s in rows]})


@api_bp.route('/vulnerabilities/<int:vuln_id>')
@login_required
@devops_required
def vulnerability_detail(vuln_id):
    v = _get_or_404(Vulnerability, vuln_id)
    return jsonify(vulnerability_dict(v, detail=True))


@api_bp.route('/vulnerabilities/<int:vuln_id>/acknowledge', methods=['POST'])
@login_required
@devops_required
def vulnerability_acknowledge(vuln_id):
    v = _get_or_404(Vulnerability, vuln_id)
    if not v.acknowledged:
        v.acknowledged = True
        v.acknowledged_at = datetime.now(timezone.utc)
        v.acknowledged_by = current_user.id
        db.session.commit()
        AuditLog.log('vuln_acknowledged', 'vulnerability', v.id,
                     user_id=current_user.id, ip_address=request.remote_addr,
                     details={'cve_id': v.cve_id, 'source': v.source})
    return jsonify(vulnerability_dict(v, detail=True))


@api_bp.route('/vulnerabilities/<int:vuln_id>/unacknowledge', methods=['POST'])
@login_required
@devops_required
def vulnerability_unacknowledge(vuln_id):
    v = _get_or_404(Vulnerability, vuln_id)
    if v.acknowledged:
        v.acknowledged = False
        v.acknowledged_at = None
        v.acknowledged_by = None
        db.session.commit()
        AuditLog.log('vuln_unacknowledged', 'vulnerability', v.id,
                     user_id=current_user.id, ip_address=request.remote_addr,
                     details={'cve_id': v.cve_id, 'source': v.source})
    return jsonify(vulnerability_dict(v, detail=True))


@api_bp.route('/vulnerabilities/scan', methods=['POST'])
@login_required
@devops_required
def vulnerability_scan():
    """Pull from OSV.dev now rather than waiting for the daily job.

    Synchronous, like /tickets/intake/run — a handful of packages against OSV is
    seconds, not minutes, and the caller wants the fresh counts back.
    """
    from ...services.vuln_scan import scan_all, scan_enabled

    if not scan_enabled():
        return jsonify({'error': 'Vulnerability scanning is disabled.'}), 503

    summary = scan_all()
    AuditLog.log('vuln_scan_run', 'vulnerability', None,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'created': summary['created'], 'updated': summary['updated'],
                          'sources': summary['sources']})
    return jsonify(summary)


@api_bp.route('/vulnerabilities/export.csv')
@login_required
@devops_required
def vulnerabilities_export():
    """Filtered findings as CSV. A GET (CSRF-exempt); the SPA fetches it as a
    credentialed blob so the session cookie is sent cross-origin."""
    query = Vulnerability.query

    severity = (request.args.get('severity') or '').strip()
    if severity and severity != 'all':
        query = query.filter_by(severity=severity)
    source = (request.args.get('source') or '').strip()
    if source and source != 'all':
        query = query.filter_by(source=source)
    acknowledged = (request.args.get('acknowledged') or '').strip().lower()
    if acknowledged == 'true':
        query = query.filter_by(acknowledged=True)
    elif acknowledged in ('false', 'open'):
        query = query.filter_by(acknowledged=False)

    rows = sorted(
        query.order_by(Vulnerability.published.desc().nullslast()).all(),
        key=lambda v: -v.severity_rank)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['source', 'technology', 'cve_id', 'severity',
                     'affected_version', 'fixed_version', 'published', 'url',
                     'acknowledged', 'first_seen'])
    for v in rows:
        writer.writerow([
            v.source, v.technology or '', v.cve_id, v.severity,
            v.affected_version or '', v.fixed_version or '',
            v.published.isoformat() if v.published else '',
            v.url or '', 'yes' if v.acknowledged else 'no',
            v.first_seen.isoformat() if v.first_seen else '',
        ])

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=vulnerabilities.csv'},
    )
