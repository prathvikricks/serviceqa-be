"""The central AWS Secrets Manager integration.

One set of global AWS credentials (configured in Admin → Settings) is used to
list and read every secret in the account, independent of any project. Projects
then *reference* AWS secrets (see models/project_aws_secret.py); values are never
stored — they're fetched live from AWS on reveal, so AWS stays the source of
truth.

Mirrors graph_mail: this module owns the integration, reads its credentials
through get_setting (so an admin can rotate them without a restart), and caches
one AWSManager that a settings change clears via reset_cache().
"""
import threading

_manager = None
_manager_lock = threading.Lock()


class SecretsManagerUnavailable(Exception):
    """The global AWS credentials are not configured."""


def _cfg(key):
    """Settings first, environment second — see models/setting.get_setting."""
    from ..models.setting import get_setting
    return get_setting(key)


def default_region():
    return _cfg('AWS_REGION') or 'us-east-1'


def is_enabled():
    """True only when both halves of the credential are present."""
    return bool(_cfg('AWS_ACCESS_KEY_ID') and _cfg('AWS_SECRET_ACCESS_KEY'))


def reset_cache():
    """Drop the cached manager. For a credential rotation and for tests."""
    global _manager
    with _manager_lock:
        _manager = None


def get_manager():
    """The central AWSManager, built lazily from the global settings and cached.

    Raises SecretsManagerUnavailable when the credentials aren't configured, so
    callers can return a clear 4xx rather than a boto credential error.
    """
    global _manager
    if not is_enabled():
        raise SecretsManagerUnavailable(
            'AWS Secrets Manager is not configured. Set the credentials in '
            'Admin → Settings → AWS Secrets Manager.')
    with _manager_lock:
        if _manager is None:
            from .aws_manager import AWSManager
            _manager = AWSManager({
                'region': default_region(),
                'access_key_id': _cfg('AWS_ACCESS_KEY_ID'),
                'secret_access_key': _cfg('AWS_SECRET_ACCESS_KEY'),
            })
        return _manager


def check_connection():
    """Probe connectivity by listing secrets. Returns {region, secret_count}.

    Doubles as the "does listing actually work" check for the settings status
    endpoint — needs no IAM permission beyond what the feature already requires.
    Errors surface to the caller.
    """
    manager = get_manager()
    secrets = manager.list_all_secrets()
    return {'region': default_region(), 'secret_count': len(secrets)}
