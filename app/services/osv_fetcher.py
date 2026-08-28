"""OSV.dev fetcher — the single gate for outbound vulnerability data.

Ported from VulnWatch's Go ``internal/fetchers/osv.go`` plus its severity
normalization and CVE-extraction helpers. Follows the ``graph_mail`` convention:
stdlib ``urllib`` only, so enabling this needs no new package and the image is
not rebuilt (``requests`` is deliberately absent from requirements).

One public call: ``query_osv(ecosystem, package)`` returns a list of normalized
finding dicts (already shaped for the ``Vulnerability`` columns). The scan
orchestration in ``vuln_scan`` upserts them.
"""
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import current_app

logger = logging.getLogger(__name__)

_OSV_URL = 'https://api.osv.dev/v1/query'
# A hung call must not tie up the single gunicorn worker — same rationale as
# graph_mail's timeout.
_TIMEOUT = 20
# OSV returns findings newest-context but unbounded; Django alone is hundreds.
# Bound page-following so one noisy package can't run the scan forever. The
# per-source total is additionally capped by VULN_MAX_PER_SOURCE in the caller.
_MAX_PAGES = 15

_TAG = re.compile(r'<[^>]+>')
_CVE = re.compile(r'CVE-\d{4}-\d{4,7}')


class OSVError(Exception):
    """OSV could not be reached or returned an unusable response."""


def _max_per_source():
    try:
        return int(current_app.config.get('VULN_MAX_PER_SOURCE', 200))
    except RuntimeError:
        # Outside an app context (e.g. a unit test calling helpers directly).
        return 200


def query_osv(ecosystem, package, max_results=None):
    """All OSV findings for one package, normalized to finding dicts.

    Paginates via OSV's ``next_page_token`` up to ``_MAX_PAGES`` / ``max_results``.
    Raises ``OSVError`` on a transport or decode failure so the caller can record
    the source as errored without sinking the whole scan.
    """
    cap = max_results or _max_per_source()
    vulns = []
    token = None
    pages = 0

    while pages < _MAX_PAGES and len(vulns) < cap:
        payload = {'package': {'name': package, 'ecosystem': ecosystem}}
        if token:
            payload['page_token'] = token
        body = _post(payload)
        vulns.extend(body.get('vulns') or [])
        token = body.get('next_page_token')
        pages += 1
        if not token:
            break

    if token:
        logger.warning('OSV scan for %s/%s stopped at %d findings; more remain',
                       ecosystem, package, len(vulns))

    findings = []
    for vuln in vulns[:cap]:
        findings.append(_to_fields(vuln))
    return findings


def _post(payload):
    """One OSV POST, urllib only, errors normalized into OSVError."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _OSV_URL, data=data,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8', 'replace')[:200]
        except Exception:
            pass
        raise OSVError(f'OSV returned HTTP {exc.code}: {detail}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSVError(f'Could not reach OSV: {exc}') from exc
    except ValueError as exc:
        raise OSVError('OSV returned a non-JSON response.') from exc


# ─── Normalization (ported from parser/normalize.go, extract.go, sanitize.go) ──

def normalize_severity_string(value):
    """Map a source's severity label onto our lower-case enum."""
    s = (value or '').strip().upper()
    if s == 'CRITICAL':
        return 'critical'
    if s in ('HIGH', 'IMPORTANT'):
        return 'high'
    if s in ('MEDIUM', 'MODERATE', 'AVERAGE'):
        return 'medium'
    if s in ('LOW', 'MINOR', 'INFORMATIONAL', 'INFO'):
        return 'low'
    return 'unknown'


def normalize_cvss_score(score):
    """CVSS v3.1 qualitative bands (per NIST), mirroring NormalizeCVSSScore."""
    try:
        score = float(score)
    except (TypeError, ValueError):
        return 'unknown'
    if score >= 9.0:
        return 'critical'
    if score >= 7.0:
        return 'high'
    if score >= 4.0:
        return 'medium'
    if score > 0.0:
        return 'low'
    return 'unknown'


def _sanitize(text):
    """Strip HTML so a stored description is never markup."""
    if not text:
        return ''
    return _TAG.sub('', text).strip()


def _cve_id(vuln):
    """Prefer a CVE alias, then a CVE in the text, else the OSV id itself.

    OSV ids are usually GHSA-*; the fallback means cve_id can be a GHSA/OSV id —
    fine as a dedup key, but the UI must not assume a 'CVE-' prefix.
    """
    for alias in vuln.get('aliases') or []:
        if isinstance(alias, str) and alias.startswith('CVE-'):
            return alias
    match = _CVE.search(f"{vuln.get('summary', '')} {vuln.get('details', '')}")
    if match:
        return match.group(0)
    return vuln.get('id') or ''


def _severity(vuln):
    """database_specific → ecosystem_specific → unknown, matching osv.go."""
    db_specific = vuln.get('database_specific') or {}
    if db_specific.get('severity'):
        return normalize_severity_string(db_specific['severity'])
    eco_specific = vuln.get('ecosystem_specific') or {}
    if eco_specific.get('severity'):
        return normalize_severity_string(eco_specific['severity'])
    return 'unknown'


def _url(vuln):
    """First ADVISORY/WEB reference, else the first reference, else empty."""
    refs = vuln.get('references') or []
    for ref in refs:
        if ref.get('type') in ('ADVISORY', 'WEB') and ref.get('url'):
            return ref['url']
    if refs and refs[0].get('url'):
        return refs[0]['url']
    return ''


def _affected_version(vuln):
    """Earliest introduced version (as '>=X'), else the first listed version."""
    for affected in vuln.get('affected') or []:
        for rng in affected.get('ranges') or []:
            for event in rng.get('events') or []:
                introduced = event.get('introduced')
                if introduced and introduced != '0':
                    return f'>={introduced}'
        versions = affected.get('versions') or []
        if versions:
            return versions[0]
    return ''


def _fixed_version(vuln):
    """Earliest fixed version across the affected ranges."""
    for affected in vuln.get('affected') or []:
        for rng in affected.get('ranges') or []:
            for event in rng.get('events') or []:
                if event.get('fixed'):
                    return event['fixed']
    return ''


def _published(vuln):
    """Parse OSV's RFC3339 'published' into NAIVE UTC.

    Stored naive so the serializer's _utc re-stamps the 'Z' consistently; mixing
    aware/naive in one column also breaks SQLite datetime comparisons in tests.
    """
    raw = vuln.get('published') or vuln.get('modified')
    if not raw:
        return None
    text = raw.strip()
    # Python < 3.11 fromisoformat rejects a trailing 'Z'.
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _to_fields(vuln):
    """One OSV vuln → the column dict for a Vulnerability row."""
    desc = _sanitize(vuln.get('summary')) or _sanitize(vuln.get('details'))
    return {
        'cve_id': _cve_id(vuln),
        'severity': _severity(vuln),
        'description': desc or None,
        'url': _url(vuln) or None,
        'published': _published(vuln),
        'affected_version': _affected_version(vuln) or None,
        'fixed_version': _fixed_version(vuln) or None,
    }
