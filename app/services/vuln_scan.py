"""Vulnerability scan orchestration.

Ties the OSV fetcher to the database: for each configured source, fetch findings
and upsert them, then record the source's outcome for the status panel. This is
the port of VulnWatch's scheduler run loop, minus the alerting — a scan only
populates the listing.

``scan_all`` is called both by the daily scheduler job and by the "Scan now"
endpoint. It never raises: a single bad source is isolated so the rest still run.
"""
import json
import logging

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models.vulnerability import Vulnerability, VulnSourceStatus
from . import nvd_fetcher, osv_fetcher

logger = logging.getLogger(__name__)


def scan_enabled():
    """OSV needs no credentials, so this defaults on; the flag is an off switch."""
    return bool(current_app.config.get('VULN_SCAN_ENABLED', True))


def load_sources():
    """The configured OSV packages to track. JSON, not YAML — no new dependency."""
    path = current_app.config['VULN_SOURCES_PATH']
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.error('Could not read vuln sources at %s: %s', path, exc)
        return []
    return data.get('sources') or []


def upsert_finding(source_cfg, fields):
    """Insert a new finding or refresh an existing one, keyed by (source, cve_id).

    On an existing row only the *volatile* fields are touched — a re-scan must
    never reset ``first_seen`` or clear an acknowledgement, or the daily job
    would silently undo a triager's work every night.

    Returns 'created' or 'updated'.
    """
    source = source_cfg['name']
    cve_id = fields.get('cve_id')
    if not cve_id:
        return None

    existing = Vulnerability.query.filter_by(source=source, cve_id=cve_id).first()
    if existing:
        existing.severity = fields['severity']
        existing.description = fields['description']
        existing.url = fields['url']
        existing.published = fields['published']
        existing.affected_version = fields['affected_version']
        existing.fixed_version = fields['fixed_version']
        existing.technology = source_cfg.get('technology')
        # last_seen is bumped by the onupdate hook.
        return 'updated'

    vuln = Vulnerability(
        source=source,
        technology=source_cfg.get('technology'),
        cve_id=cve_id,
        severity=fields['severity'],
        description=fields['description'],
        url=fields['url'],
        published=fields['published'],
        affected_version=fields['affected_version'],
        fixed_version=fields['fixed_version'],
    )
    db.session.add(vuln)
    try:
        db.session.flush()
    except IntegrityError:
        # Lost a race with a concurrent scan; the unique index is what makes this
        # safe rather than the check above. Re-query and treat as an update.
        db.session.rollback()
        existing = Vulnerability.query.filter_by(source=source, cve_id=cve_id).first()
        if existing:
            existing.severity = fields['severity']
            existing.description = fields['description']
            existing.url = fields['url']
            existing.published = fields['published']
            existing.affected_version = fields['affected_version']
            existing.fixed_version = fields['fixed_version']
            existing.technology = source_cfg.get('technology')
        return 'updated'
    return 'created'


def _record_status(source_cfg, status, found_count, error=None):
    """Upsert the per-source status row that feeds the panel."""
    from datetime import datetime, timezone

    row = VulnSourceStatus.query.filter_by(source=source_cfg['name']).first()
    if row is None:
        row = VulnSourceStatus(source=source_cfg['name'])
        db.session.add(row)
    row.technology = source_cfg.get('technology')
    # NVD sources have no ecosystem/package — show the feed and its keyword so the
    # panel reads sensibly for both source types.
    if (source_cfg.get('type') or 'osv') == 'nvd':
        row.ecosystem = 'NVD'
        row.package = source_cfg.get('keyword')
    else:
        row.ecosystem = source_cfg.get('ecosystem')
        row.package = source_cfg.get('package')
    row.last_scanned_at = datetime.now(timezone.utc)
    row.status = status
    row.error = error
    row.found_count = found_count


def _fetch_findings(source_cfg):
    """Dispatch to the right fetcher by source type. Raises the fetcher's error."""
    source_type = source_cfg.get('type') or 'osv'
    if source_type == 'nvd':
        return nvd_fetcher.query_nvd(source_cfg['keyword'],
                                     min_severity=source_cfg.get('min_severity'))
    return osv_fetcher.query_osv(source_cfg['ecosystem'], source_cfg['package'])


def scan_source(source_cfg):
    """Scan one source. Returns a per-source summary; records its status row.

    Any failure is caught and turned into an 'error' status rather than raised,
    so ``scan_all`` can keep going.
    """
    source = source_cfg['name']
    try:
        findings = _fetch_findings(source_cfg)
    except (osv_fetcher.OSVError, nvd_fetcher.NVDError) as exc:
        logger.warning('Vuln scan source %s failed: %s', source, exc)
        _record_status(source_cfg, 'error', 0, error=str(exc)[:500])
        db.session.commit()
        return {'source': source, 'created': 0, 'updated': 0, 'found': 0,
                'error': str(exc)[:500]}

    created = updated = 0
    for fields in findings:
        result = upsert_finding(source_cfg, fields)
        if result == 'created':
            created += 1
        elif result == 'updated':
            updated += 1

    _record_status(source_cfg, 'ok', created + updated)
    db.session.commit()
    return {'source': source, 'created': created, 'updated': updated,
            'found': len(findings), 'error': None}


def scan_all():
    """Scan every configured source. Never raises.

    Returns an aggregate summary the "Scan now" endpoint can echo back.
    """
    sources = load_sources()
    results = []
    for source_cfg in sources:
        try:
            results.append(scan_source(source_cfg))
        except Exception as exc:  # defensive: a bad row must not sink the batch
            logger.exception('Vuln scan source %s crashed', source_cfg.get('name'))
            db.session.rollback()
            results.append({'source': source_cfg.get('name'), 'created': 0,
                            'updated': 0, 'found': 0, 'error': str(exc)[:500]})

    created = sum(r['created'] for r in results)
    updated = sum(r['updated'] for r in results)
    return {
        'sources': len(results),
        'created': created,
        'updated': updated,
        'found': sum(r['found'] for r in results),
        'results': results,
    }
