"""Git repository provisioning.

Creates repositories on GitHub or GitLab from an approved 'repo' request. The
approver picks the provider at approval time; tokens and target namespaces come
from backend env vars (global, not per-user), mirroring how SECRET_KEY/CRED_KEY
are configured:

    GITHUB_TOKEN      personal/org access token (repo scope)
    GITHUB_ORG        org to create under; blank => the token owner's account

    GITLAB_TOKEN      personal/group access token (api scope)
    GITLAB_URL        base URL (default https://gitlab.com)
    GITLAB_NAMESPACE_ID   numeric group/namespace id; blank => the token owner

Uses the stdlib urllib so this needs no extra dependency (the image is not
rebuilt for a new package). Network/validation failures raise RuntimeError with
a short, user-facing message — the approve endpoint surfaces it on the request.
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 20  # seconds — a hung call must not tie up the single worker.


def configured_providers() -> list:
    """Which providers have a token set, so the UI only offers real options."""
    providers = []
    if os.environ.get('GITHUB_TOKEN'):
        providers.append('github')
    if os.environ.get('GITLAB_TOKEN'):
        providers.append('gitlab')
    return providers


def create_repo(provider: str, name: str, description: str = '',
                private: bool = True) -> str:
    """Create a repository and return its web URL. Raises RuntimeError on failure."""
    if not name:
        raise RuntimeError('Repository name is required.')
    if provider == 'github':
        return _create_github(name, description, private)
    if provider == 'gitlab':
        return _create_gitlab(name, description, private)
    raise RuntimeError(f'Unsupported git provider: {provider!r}')


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        detail = _extract_error(e)
        logger.error('Git API %s -> %s: %s', url, e.code, detail)
        raise RuntimeError(detail) from e
    except urllib.error.URLError as e:
        logger.error('Git API %s unreachable: %s', url, e)
        raise RuntimeError(f'Could not reach the Git provider: {e.reason}') from e


def _extract_error(err: urllib.error.HTTPError) -> str:
    """Best-effort human message from a provider error body."""
    try:
        data = json.loads(err.read().decode() or '{}')
    except (ValueError, OSError):
        data = {}
    # GitHub: {"message": "...", "errors": [{"message": "..."}]}
    if isinstance(data, dict):
        errs = data.get('errors')
        if isinstance(errs, list) and errs:
            first = errs[0]
            if isinstance(first, dict) and first.get('message'):
                return f"{data.get('message', 'Error')}: {first['message']}"
        # GitLab: {"message": {...}} or {"message": ["..."]} or {"error": "..."}
        msg = data.get('message') or data.get('error')
        if isinstance(msg, dict):
            return '; '.join(f"{k}: {', '.join(v) if isinstance(v, list) else v}"
                             for k, v in msg.items())
        if isinstance(msg, list):
            return '; '.join(str(m) for m in msg)
        if msg:
            return str(msg)
    return f'Git provider returned HTTP {err.code}.'


def _create_github(name: str, description: str, private: bool) -> str:
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        raise RuntimeError('GitHub is not configured (GITHUB_TOKEN missing).')
    org = (os.environ.get('GITHUB_ORG') or '').strip()
    url = (f'https://api.github.com/orgs/{org}/repos' if org
           else 'https://api.github.com/user/repos')
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'envmanager',
        'Content-Type': 'application/json',
    }
    data = _post_json(url, headers, {
        'name': name,
        'description': description or '',
        'private': bool(private),
    })
    return data.get('html_url') or ''


def _create_gitlab(name: str, description: str, private: bool) -> str:
    token = os.environ.get('GITLAB_TOKEN')
    if not token:
        raise RuntimeError('GitLab is not configured (GITLAB_TOKEN missing).')
    base = (os.environ.get('GITLAB_URL') or 'https://gitlab.com').rstrip('/')
    payload = {
        'name': name,
        'description': description or '',
        'visibility': 'private' if private else 'public',
    }
    namespace_id = (os.environ.get('GITLAB_NAMESPACE_ID') or '').strip()
    if namespace_id:
        payload['namespace_id'] = int(namespace_id)
    headers = {
        'PRIVATE-TOKEN': token,
        'User-Agent': 'envmanager',
        'Content-Type': 'application/json',
    }
    data = _post_json(f'{base}/api/v4/projects', headers, payload)
    return data.get('web_url') or ''
