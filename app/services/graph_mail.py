"""Microsoft Graph mailbox access — the single gate for all Microsoft I/O.

Mirrors git_manager: stdlib urllib only, so enabling this needs no new package
and the image is not rebuilt. The client-credentials flow is one form POST; msal
exists for interactive, device-code and on-behalf-of flows we do not have.

Permissions are Mail.Read + Mail.Send, application-level, restricted to one
mailbox by an Application Access Policy. Nothing here writes to the mailbox —
marking a message handled is a local database concern (see EmailIntakeMessage),
not a PATCH that would need Mail.ReadWrite.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import current_app

logger = logging.getLogger(__name__)

# A hung call must not tie up the single gunicorn worker — same rationale as
# git_manager's timeout.
_TIMEOUT = 20
_GRAPH = 'https://graph.microsoft.com/v1.0'
_PAGE_SIZE = 25
_MAX_PAGES = 8          # hard ceiling: 200 messages per poll

_token = None           # {'value': str, 'expires_at': float}
_token_lock = threading.Lock()


class MailUnavailable(Exception):
    """The feature is not configured."""


class MailError(Exception):
    """Transient — worth retrying on the next tick."""


class MailPermanentError(MailError):
    """Configuration or consent is wrong. Retrying will not help."""


def _cfg(key):
    return current_app.config.get(key)


def is_enabled():
    """True only when every credential needed to reach the mailbox is present."""
    return all(_cfg(k) for k in ('GRAPH_TENANT_ID', 'GRAPH_CLIENT_ID',
                                 'GRAPH_CLIENT_SECRET', 'DEVOPS_MAILBOX'))


def reset_token_cache():
    """Drop the cached token. For tests and for a credential rotation."""
    global _token
    with _token_lock:
        _token = None


def _request(url, method='GET', headers=None, data=None, form=False):
    """One HTTP call, with Graph's error shape normalised into our exceptions."""
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data).encode()
            hdrs['Content-Type'] = 'application/json'

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise _classify(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MailError(f'Could not reach Microsoft Graph: {exc}') from exc
    except ValueError as exc:
        raise MailError('Graph returned a non-JSON response.') from exc


def _classify(exc):
    """Turn an HTTPError into the right exception, carrying Graph's own message."""
    try:
        payload = json.loads(exc.read().decode('utf-8', 'replace'))
        message = payload.get('error', {}).get('message') or str(exc)
    except Exception:
        message = str(exc)
    message = message[:300]

    if exc.code in (401, 403, 404, 400):
        # 403 here is usually missing admin consent or an Application Access
        # Policy that does not cover this mailbox — both need a human, so
        # retrying every two minutes would just spam the log.
        return MailPermanentError(f'Graph {exc.code}: {message}')
    return MailError(f'Graph {exc.code}: {message}')


def _access_token(force=False):
    """App-only token, cached in-process and refreshed just before expiry.

    The poller runs every couple of minutes; without caching that is hundreds of
    pointless token calls a day. Guarded by a lock because the scheduler uses a
    thread pool and the ack path may run on a request thread.
    """
    global _token
    if not is_enabled():
        raise MailUnavailable('Microsoft Graph is not configured.')

    with _token_lock:
        if not force and _token and _token['expires_at'] > time.time():
            return _token['value']

        url = (f"https://login.microsoftonline.com/{_cfg('GRAPH_TENANT_ID')}"
               "/oauth2/v2.0/token")
        payload = _request(url, method='POST', form=True, data={
            'client_id': _cfg('GRAPH_CLIENT_ID'),
            'client_secret': _cfg('GRAPH_CLIENT_SECRET'),
            'grant_type': 'client_credentials',
            'scope': 'https://graph.microsoft.com/.default',
        })
        value = payload.get('access_token')
        if not value:
            raise MailPermanentError('Token response carried no access_token.')
        _token = {'value': value,
                  'expires_at': time.time() + int(payload.get('expires_in', 3600)) - 120}
        return value


def _auth_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        # Ask Exchange for plain text rather than HTML, so the trigger matcher
        # works on prose instead of markup.
        'Prefer': 'outlook.body-content-type="text", IdType="ImmutableId"',
    }


def _get_with_retry(url):
    """GET, refreshing the token once on a 401 before giving up."""
    try:
        return _request(url, headers=_auth_headers(_access_token()))
    except MailPermanentError as exc:
        if 'Graph 401' not in str(exc):
            raise
        return _request(url, headers=_auth_headers(_access_token(force=True)))


def fetch_messages(since, limit=_PAGE_SIZE * _MAX_PAGES):
    """Inbox messages received at or after `since`, oldest first.

    Selects `uniqueBody` rather than `body`: Graph strips the quoted reply chain
    server-side, which removes most of the trigger false positives before our
    own heuristics ever run.
    """
    if not is_enabled():
        raise MailUnavailable('Microsoft Graph is not configured.')

    stamp = since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    params = urllib.parse.urlencode({
        '$select': ('id,internetMessageId,conversationId,subject,from,'
                    'toRecipients,ccRecipients,receivedDateTime,uniqueBody'),
        '$filter': f'receivedDateTime ge {stamp}',
        '$orderby': 'receivedDateTime asc',
        '$top': _PAGE_SIZE,
    })
    url = (f"{_GRAPH}/users/{urllib.parse.quote(_cfg('DEVOPS_MAILBOX'))}"
           f"/mailFolders/Inbox/messages?{params}")

    messages, pages = [], 0
    while url and pages < _MAX_PAGES and len(messages) < limit:
        payload = _get_with_retry(url)
        messages.extend(payload.get('value') or [])
        # Follow nextLink verbatim — it carries an opaque skip token.
        url = payload.get('@odata.nextLink')
        pages += 1

    if url:
        logger.warning('Mailbox poll stopped at %s messages; more remain', len(messages))
    return messages[:limit]


def send_mail(to, subject, body_text, in_reply_to=None):
    """Send as the mailbox. Raises MailError on failure — callers must not care."""
    if not is_enabled():
        raise MailUnavailable('Microsoft Graph is not configured.')

    message = {
        'subject': subject,
        'body': {'contentType': 'Text', 'content': body_text},
        'toRecipients': [{'emailAddress': {'address': to}}],
    }
    if in_reply_to:
        # Makes the acknowledgement thread under the sender's original rather
        # than opening a new conversation in their client.
        message['internetMessageHeaders'] = [
            {'name': 'In-Reply-To', 'value': in_reply_to},
            {'name': 'References', 'value': in_reply_to},
        ]

    url = (f"{_GRAPH}/users/{urllib.parse.quote(_cfg('DEVOPS_MAILBOX'))}/sendMail")
    _request(url, method='POST',
             headers={'Authorization': f'Bearer {_access_token()}'},
             data={'message': message, 'saveToSentItems': True})


def default_since():
    """Cold-start window when the intake ledger is empty."""
    minutes = _cfg('MAIL_INTAKE_LOOKBACK_MINUTES') or 60
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)
