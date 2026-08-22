"""Application-level settings, editable by an admin at runtime.

Everything else in this app is configured by environment variable, which is the
right default for infrastructure — but it means changing a key needs a shell, a
file edit and a container restart. For credentials an admin is expected to
rotate (the LLM key, the mail app secret), that is friction in the wrong place.

Values are Fernet-encrypted at rest with the same CRED_KEY as project secrets
and cloud credentials, and are never returned by the API — only whether a value
is set, plus a masked hint.

Precedence is deliberate: a value stored here OVERRIDES the environment. The
env var stays the bootstrap and the fallback, so a deploy that has never opened
the settings page behaves exactly as before.
"""
from datetime import datetime, timezone

from ..extensions import db


class Setting(db.Model):
    __tablename__ = 'settings'

    # Only keys listed here can be written through the API. An open key/value
    # store reachable by HTTP is a way to overwrite SECRET_KEY by accident.
    EDITABLE = {
        'GEMINI_API_KEY': {'label': 'Gemini API key', 'secret': True,
                           'help': 'Enables the chat assistant and ticket summaries.'},
        'GEMINI_MODEL': {'label': 'Gemini model', 'secret': False,
                         'help': 'Defaults to gemini-2.5-flash.'},
    }

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False, unique=True, index=True)
    # Ciphertext. Never read directly — use get_value()/set_value().
    value = db.Column(db.Text, nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    editor = db.relationship('User', foreign_keys=[updated_by])

    # --- the single crypto boundary ----------------------------------------

    def get_value(self):
        from ..services.crypto import decrypt
        try:
            return decrypt(self.value)
        except Exception:
            # Rotated or wrong CRED_KEY. Behave as if unset rather than 500 —
            # the settings page shows it as not configured, which is the truth
            # from the app's point of view.
            return ''

    def set_value(self, plaintext):
        from ..services.crypto import encrypt
        self.value = encrypt(plaintext or '')

    @property
    def is_secret(self):
        return self.EDITABLE.get(self.key, {}).get('secret', True)

    @property
    def hint(self):
        """A masked tail, so an admin can tell which key is installed.

        Four characters of a long random token is not enough to be useful to an
        attacker, and is enough to distinguish two keys.
        """
        value = self.get_value()
        if not value:
            return None
        if not self.is_secret:
            return value
        return f'…{value[-4:]}' if len(value) > 8 else '…'

    def __repr__(self):
        return f'<Setting {self.key}>'


def get_setting(key, default=None):
    """Resolve a setting: database first, then app config (env), then default.

    Called on every request that touches a gated feature, so it must stay cheap
    and must never raise — a broken settings row should degrade to the env var,
    not take the feature down.
    """
    from flask import current_app
    try:
        row = Setting.query.filter_by(key=key).first()
        if row is not None:
            value = row.get_value()
            if value:
                return value
    except Exception:  # pragma: no cover - table missing during early boot
        pass
    return current_app.config.get(key, default)
