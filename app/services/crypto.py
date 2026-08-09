"""Symmetric encryption for credentials at rest (Fernet).

The single crypto boundary behind `Project.get/set_provider_config`. The key
comes from `CRED_KEY` (a urlsafe-base64 Fernet key); when unset it's derived
deterministically from `SECRET_KEY` so dev works with no extra config. In
production set `CRED_KEY` explicitly — rotating `SECRET_KEY` without it would
orphan previously-encrypted values.
"""
import base64
import hashlib

from flask import current_app
from cryptography.fernet import Fernet


def _fernet():
    key = current_app.config.get('CRED_KEY')
    if not key:
        secret = (current_app.config.get('SECRET_KEY') or 'dev').encode()
        key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    elif isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Encrypt a string; returns a urlsafe token (str)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by encrypt()."""
    return _fernet().decrypt(token.encode()).decode()
