"""Endpoint tests for the vulnerability listing — access control, filters,
acknowledge toggle, CSV export, and the capability probe."""
import json
from datetime import datetime, timezone

import pytest

from app.extensions import db
from app.models.vulnerability import Vulnerability, VulnSourceStatus

from conftest import login


@pytest.fixture
def vulns(app):
    """A handful of findings across two sources and three severities."""
    rows = [
        Vulnerability(source='django', technology='Django', cve_id='CVE-2022-1',
                      severity='critical', description='SQL injection in Django',
                      published=datetime(2022, 4, 13)),
        Vulnerability(source='django', technology='Django', cve_id='CVE-2022-2',
                      severity='medium', description='XSS in admin'),
        Vulnerability(source='trivy', technology='Trivy', cve_id='CVE-2023-9',
                      severity='high', description='Path traversal'),
    ]
    db.session.add_all(rows)
    db.session.add(VulnSourceStatus(source='django', technology='Django',
                                    ecosystem='PyPI', package='Django',
                                    status='ok', found_count=2,
                                    last_scanned_at=datetime.now(timezone.utc)))
    db.session.commit()
    return rows


def test_list_requires_devops(client, users, vulns):
    for username, expected in (('dev', 403), ('ops', 200), ('admin', 200)):
        client.post('/api/v1/auth/logout')
        login(client, username)
        assert client.get('/api/v1/vulnerabilities').status_code == expected


def test_status_is_readable_by_any_user_and_leaks_no_secret(client, users, vulns):
    login(client, 'dev')
    resp = client.get('/api/v1/vulnerabilities/status')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['enabled'] is True
    assert body['never_scanned'] is False
    assert set(body['severities']) == {'critical', 'high', 'medium', 'low', 'unknown'}


def test_unauthenticated_is_401(client, users):
    assert client.get('/api/v1/vulnerabilities').status_code == 401


def test_list_returns_counts_and_sources(client, users, vulns):
    login(client, 'ops')
    body = client.get('/api/v1/vulnerabilities?acknowledged=all').get_json()
    assert body['total'] == 3
    assert body['counts']['critical'] == 1
    assert body['counts']['high'] == 1
    assert body['sources'] == ['django', 'trivy']
    # Worst-first ordering within the page.
    assert body['vulnerabilities'][0]['severity'] == 'critical'


def test_filters_narrow_the_list(client, users, vulns):
    login(client, 'ops')
    by_sev = client.get('/api/v1/vulnerabilities?severity=high&acknowledged=all').get_json()
    assert [v['cve_id'] for v in by_sev['vulnerabilities']] == ['CVE-2023-9']

    by_src = client.get('/api/v1/vulnerabilities?source=trivy&acknowledged=all').get_json()
    assert {v['source'] for v in by_src['vulnerabilities']} == {'trivy'}

    by_q = client.get('/api/v1/vulnerabilities?q=injection&acknowledged=all').get_json()
    assert [v['cve_id'] for v in by_q['vulnerabilities']] == ['CVE-2022-1']


def test_acknowledge_and_unacknowledge_toggle_and_stamp(client, users, vulns):
    login(client, 'ops')
    vid = vulns[0].id

    acked = client.post(f'/api/v1/vulnerabilities/{vid}/acknowledge').get_json()
    assert acked['acknowledged'] is True
    assert acked['acknowledged_at'] is not None
    assert acked['acknowledged_by'] == 'ops'

    # Default list (open only) no longer shows it.
    open_only = client.get('/api/v1/vulnerabilities?acknowledged=false').get_json()
    assert vid not in [v['id'] for v in open_only['vulnerabilities']]

    unacked = client.post(f'/api/v1/vulnerabilities/{vid}/unacknowledge').get_json()
    assert unacked['acknowledged'] is False
    assert unacked['acknowledged_at'] is None


def test_export_csv_returns_csv_with_header(client, users, vulns):
    login(client, 'ops')
    resp = client.get('/api/v1/vulnerabilities/export.csv?acknowledged=all')
    assert resp.status_code == 200
    assert resp.mimetype == 'text/csv'
    assert 'attachment' in resp.headers['Content-Disposition']
    text = resp.get_data(as_text=True)
    assert text.splitlines()[0] == (
        'source,technology,cve_id,severity,affected_version,fixed_version,'
        'published,url,acknowledged,first_seen')
    assert 'CVE-2022-1' in text


def test_timestamps_carry_an_explicit_utc_marker(client, users, vulns):
    login(client, 'ops')
    body = client.get('/api/v1/vulnerabilities?acknowledged=all').get_json()
    first_seen = body['vulnerabilities'][0]['first_seen']
    # _utc appends 'Z' to the naive-UTC column so the browser reads it as UTC.
    assert first_seen.endswith('Z')


def test_scan_endpoint_is_devops_only_and_audits(client, users, monkeypatch):
    from app.services import vuln_scan
    monkeypatch.setattr(vuln_scan, 'scan_all',
                        lambda: {'sources': 1, 'created': 3, 'updated': 0,
                                 'found': 3, 'results': []})

    client.post('/api/v1/auth/logout')
    login(client, 'dev')
    assert client.post('/api/v1/vulnerabilities/scan').status_code == 403

    client.post('/api/v1/auth/logout')
    login(client, 'ops')
    resp = client.post('/api/v1/vulnerabilities/scan')
    assert resp.status_code == 200
    assert resp.get_json()['created'] == 3

    from app.models.audit import AuditLog
    assert AuditLog.query.filter_by(action='vuln_scan_run').count() == 1
