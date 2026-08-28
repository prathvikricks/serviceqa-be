"""Unit tests for the OSV fetcher/normalization and scan upsert — no network.

The one OSV call is monkeypatched with canned response fields, so these cover the
pure logic: severity normalization, CVE extraction, version parsing, and the
dedup upsert that must preserve a triager's acknowledgement across re-scans.
"""
from datetime import datetime, timezone

from app.extensions import db
from app.models.vulnerability import Vulnerability
from app.services import nvd_fetcher, osv_fetcher, vuln_scan


def test_normalize_severity_string_maps_vendor_labels():
    assert osv_fetcher.normalize_severity_string('CRITICAL') == 'critical'
    assert osv_fetcher.normalize_severity_string('Important') == 'high'
    assert osv_fetcher.normalize_severity_string('moderate') == 'medium'
    assert osv_fetcher.normalize_severity_string('AVERAGE') == 'medium'
    assert osv_fetcher.normalize_severity_string('informational') == 'low'
    assert osv_fetcher.normalize_severity_string('something-else') == 'unknown'
    assert osv_fetcher.normalize_severity_string(None) == 'unknown'


def test_normalize_cvss_score_bands():
    assert osv_fetcher.normalize_cvss_score(9.1) == 'critical'
    assert osv_fetcher.normalize_cvss_score(7.0) == 'high'
    assert osv_fetcher.normalize_cvss_score(4.0) == 'medium'
    assert osv_fetcher.normalize_cvss_score(0.1) == 'low'
    assert osv_fetcher.normalize_cvss_score(0) == 'unknown'
    assert osv_fetcher.normalize_cvss_score('nan') == 'unknown'


def test_cve_id_falls_back_alias_then_regex_then_osv_id():
    assert osv_fetcher._cve_id({'aliases': ['GHSA-xxxx', 'CVE-2022-1234']}) == 'CVE-2022-1234'
    assert osv_fetcher._cve_id({'summary': 'fixes CVE-2021-9999 today'}) == 'CVE-2021-9999'
    assert osv_fetcher._cve_id({'id': 'GHSA-abcd-efgh-ijkl'}) == 'GHSA-abcd-efgh-ijkl'


def test_version_extraction_from_ranges():
    vuln = {
        'affected': [{
            'ranges': [{'events': [{'introduced': '3.2'}, {'fixed': '3.2.15'}]}],
            'versions': ['3.2', '3.2.1'],
        }],
    }
    assert osv_fetcher._affected_version(vuln) == '>=3.2'
    assert osv_fetcher._fixed_version(vuln) == '3.2.15'


def test_published_parses_to_naive_utc():
    dt = osv_fetcher._published({'published': '2022-04-13T00:00:33Z'})
    assert dt is not None
    assert dt.tzinfo is None
    assert (dt.year, dt.month, dt.day) == (2022, 4, 13)


def _fields(cve_id, severity='high'):
    return {
        'cve_id': cve_id, 'severity': severity, 'description': 'desc',
        'url': 'https://example.test', 'published': None,
        'affected_version': '>=1.0', 'fixed_version': '1.2',
    }


def test_upsert_creates_then_updates_preserving_ack_and_first_seen(app):
    cfg = {'name': 'django', 'technology': 'Django', 'ecosystem': 'PyPI', 'package': 'Django'}

    assert vuln_scan.upsert_finding(cfg, _fields('CVE-2022-1', 'high')) == 'created'
    db.session.commit()

    v = Vulnerability.query.filter_by(source='django', cve_id='CVE-2022-1').one()
    v.acknowledged = True
    v.acknowledged_at = datetime.now(timezone.utc)
    original_first_seen = v.first_seen
    db.session.commit()

    # A later scan re-sees the CVE with a new severity.
    assert vuln_scan.upsert_finding(cfg, _fields('CVE-2022-1', 'critical')) == 'updated'
    db.session.commit()

    v = Vulnerability.query.filter_by(source='django', cve_id='CVE-2022-1').one()
    assert v.severity == 'critical'            # volatile field updated
    assert v.acknowledged is True              # acknowledgement preserved
    assert v.first_seen == original_first_seen  # first_seen preserved


def test_scan_source_records_status_and_counts(app, monkeypatch):
    cfg = {'name': 'trivy', 'technology': 'Trivy', 'ecosystem': 'Go',
           'package': 'github.com/aquasecurity/trivy'}
    monkeypatch.setattr(osv_fetcher, 'query_osv',
                        lambda eco, pkg, **kw: [_fields('CVE-1'), _fields('CVE-2')])

    result = vuln_scan.scan_source(cfg)
    assert result == {'source': 'trivy', 'created': 2, 'updated': 0, 'found': 2, 'error': None}

    from app.models.vulnerability import VulnSourceStatus
    row = VulnSourceStatus.query.filter_by(source='trivy').one()
    assert row.status == 'ok'
    assert row.found_count == 2
    assert row.last_scanned_at is not None


def test_scan_source_records_error_without_raising(app, monkeypatch):
    cfg = {'name': 'trivy', 'ecosystem': 'Go', 'package': 'x'}

    def boom(eco, pkg, **kw):
        raise osv_fetcher.OSVError('OSV returned HTTP 500')

    monkeypatch.setattr(osv_fetcher, 'query_osv', boom)
    result = vuln_scan.scan_source(cfg)
    assert result['error']
    from app.models.vulnerability import VulnSourceStatus
    assert VulnSourceStatus.query.filter_by(source='trivy').one().status == 'error'


# ─── NVD keyword sources (WordPress / Linux / Windows) ────────────────────────

def _nvd_cve(cve_id, base_severity='HIGH', start='6.0', end='6.4'):
    return {
        'id': cve_id,
        'descriptions': [{'lang': 'en', 'value': '<p>An issue</p>'}],
        'metrics': {'cvssMetricV31': [{'cvssData': {'baseSeverity': base_severity,
                                                    'baseScore': 8.1}}]},
        'references': [{'url': 'https://nvd.nist.gov/vuln/detail/' + cve_id}],
        'configurations': [{'nodes': [{'cpeMatch': [
            {'vulnerable': True, 'versionStartIncluding': start,
             'versionEndExcluding': end}]}]}],
        'published': '2026-01-15T10:15:30.123',
    }


def test_nvd_to_fields_parses_severity_versions_and_published():
    fields = nvd_fetcher._to_fields(_nvd_cve('CVE-2026-100', 'CRITICAL'))
    assert fields['cve_id'] == 'CVE-2026-100'
    assert fields['severity'] == 'critical'
    assert fields['affected_version'] == '>=6.0'
    assert fields['fixed_version'] == '6.4'
    assert fields['description'] == 'An issue'          # HTML stripped
    assert fields['published'].tzinfo is None           # naive UTC
    assert fields['published'].year == 2026


def test_nvd_min_severity_filters_below_threshold(app, monkeypatch):
    # Page has one high and one low; min_severity=high must drop the low one.
    def fake_page(keyword, start_index, per_page):
        if per_page == 1:
            return [], 2  # the count probe
        return [_nvd_cve('CVE-2026-1', 'HIGH'), _nvd_cve('CVE-2026-2', 'LOW')], 2

    monkeypatch.setattr(nvd_fetcher, '_fetch_page', fake_page)
    monkeypatch.setattr(nvd_fetcher, '_throttle', lambda: None)

    rows = nvd_fetcher.query_nvd('microsoft windows', min_severity='high')
    assert [r['cve_id'] for r in rows] == ['CVE-2026-1']


def test_scan_source_dispatches_nvd_type(app, monkeypatch):
    cfg = {'name': 'wordpress', 'technology': 'WordPress', 'type': 'nvd',
           'keyword': 'wordpress', 'min_severity': 'medium'}
    monkeypatch.setattr(nvd_fetcher, 'query_nvd',
                        lambda kw, **k: [
                            {'cve_id': 'CVE-2026-9', 'severity': 'high',
                             'description': 'x', 'url': None, 'published': None,
                             'affected_version': None, 'fixed_version': None}])

    result = vuln_scan.scan_source(cfg)
    assert result == {'source': 'wordpress', 'created': 1, 'updated': 0,
                      'found': 1, 'error': None}
    from app.models.vulnerability import VulnSourceStatus
    row = VulnSourceStatus.query.filter_by(source='wordpress').one()
    assert row.ecosystem == 'NVD'          # panel shows the feed…
    assert row.package == 'wordpress'      # …and the keyword
