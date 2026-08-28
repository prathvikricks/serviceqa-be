"""NVD (NIST National Vulnerability Database) keyword fetcher.

Ported from VulnWatch's ``internal/fetchers/nvd.go``. OSV indexes open-source
packages by ecosystem; things like WordPress, the Linux kernel and Windows are
not packages, so they are tracked here by keyword search against NVD instead.

NVD sorts results oldest-first and has no sort parameter, so — like vurn — we
first read the total count, then fetch the last page to get the newest CVEs.

Rate limits: 5 requests / 30s without an API key, 50 / 30s with one. We sleep
between requests accordingly (``NVD_API_KEY`` is optional but strongly advised —
without it a scan of several NVD sources is slow). urllib only, no new dependency.
"""
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from flask import current_app

from . import osv_fetcher  # reuse normalize_severity_string / normalize_cvss_score / _sanitize

logger = logging.getLogger(__name__)

_NVD_URL = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
_TIMEOUT = 45
# Match vurn's pacing: safe buffer under the published limits.
_SLEEP_NO_KEY = 6.5
_SLEEP_WITH_KEY = 0.3

# Reuse the shared severity enum ordering for the min_severity gate.
_ORDER = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1, 'unknown': 0}


class NVDError(Exception):
    """NVD could not be reached, was rate limited, or returned junk."""


def _api_key():
    return current_app.config.get('NVD_API_KEY')


def _max_results():
    try:
        return int(current_app.config.get('VULN_NVD_MAX', 20))
    except (TypeError, ValueError):
        return 20


def query_nvd(keyword, min_severity=None, max_results=None):
    """Newest CVEs matching ``keyword``, normalized to finding dicts.

    Two requests: one to learn the total, one for the last (newest) page. Applies
    an optional per-source ``min_severity`` hard filter, exactly like vurn.
    """
    cap = max_results or _max_results()

    total = _fetch_page(keyword, start_index=0, per_page=1)[1]
    if total == 0:
        return []

    start = max(0, total - cap)
    _throttle()
    page = _fetch_page(keyword, start_index=start, per_page=cap)[0]

    min_rank = _ORDER.get((min_severity or '').lower(), 0)
    findings = []
    for cve in page:
        fields = _to_fields(cve)
        if not fields['cve_id']:
            continue
        if min_rank and _ORDER.get(fields['severity'], 0) < min_rank:
            continue
        findings.append(fields)
    return findings


def _throttle():
    time.sleep(_SLEEP_WITH_KEY if _api_key() else _SLEEP_NO_KEY)


def _fetch_page(keyword, start_index, per_page):
    """Return (list-of-raw-cve, total_results) for one NVD page."""
    params = urllib.parse.urlencode({
        'keywordSearch': keyword,
        'resultsPerPage': per_page,
        'startIndex': start_index,
    })
    headers = {'Accept': 'application/json', 'User-Agent': 'ServiceManager-VulnWatch/1.0'}
    key = _api_key()
    if key:
        headers['apiKey'] = key  # never logged

    req = urllib.request.Request(f'{_NVD_URL}?{params}', headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            import json
            payload = json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise NVDError(
                f'NVD rate limited (HTTP {exc.code}) — set NVD_API_KEY to raise limits'
            ) from exc
        raise NVDError(f'NVD returned HTTP {exc.code}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NVDError(f'Could not reach NVD: {exc}') from exc
    except ValueError as exc:
        raise NVDError('NVD returned a non-JSON response.') from exc

    cves = [w.get('cve', {}) for w in payload.get('vulnerabilities') or []]
    return cves, int(payload.get('totalResults') or 0)


def _severity(cve):
    """Prefer CVSS v3.1 → v3.0 → v2, matching nvd.go's extractSeverity."""
    metrics = cve.get('metrics') or {}
    for key in ('cvssMetricV31', 'cvssMetricV30'):
        entries = metrics.get(key) or []
        if entries:
            data = entries[0].get('cvssData') or {}
            if data.get('baseSeverity'):
                return osv_fetcher.normalize_severity_string(data['baseSeverity'])
            return osv_fetcher.normalize_cvss_score(data.get('baseScore'))
    v2 = metrics.get('cvssMetricV2') or []
    if v2:
        return osv_fetcher.normalize_cvss_score((v2[0].get('cvssData') or {}).get('baseScore'))
    return 'unknown'


def _description(cve):
    for d in cve.get('descriptions') or []:
        if d.get('lang') == 'en':
            return osv_fetcher._sanitize(d.get('value'))
    return ''


def _url(cve):
    refs = cve.get('references') or []
    return refs[0].get('url', '') if refs else ''


def _versions(cve):
    """(affected, fixed) from the first vulnerable CPE match, like extractNVDVersions."""
    for conf in cve.get('configurations') or []:
        for node in conf.get('nodes') or []:
            for match in node.get('cpeMatch') or []:
                if not match.get('vulnerable'):
                    continue
                fixed = match.get('versionEndExcluding') or ''
                start = match.get('versionStartIncluding') or ''
                affected = f'>={start}' if start else ''
                if affected or fixed:
                    return affected, fixed
    return '', ''


def _published(cve):
    """NVD stamps are UTC without a timezone marker — parse and keep naive UTC."""
    raw = cve.get('published')
    if not raw:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # Fall back to the ISO parser (handles a trailing Z / offset).
    text = raw[:-1] + '+00:00' if raw.endswith('Z') else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _to_fields(cve):
    affected, fixed = _versions(cve)
    return {
        'cve_id': cve.get('id') or '',
        'severity': _severity(cve),
        'description': _description(cve) or None,
        'url': _url(cve) or None,
        'published': _published(cve),
        'affected_version': affected or None,
        'fixed_version': fixed or None,
    }
