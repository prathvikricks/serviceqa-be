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

    # Integrations, in the order they appear on the settings index. Grouping
    # lives here rather than being inferred client-side from key prefixes —
    # DEVOPS_MAILBOX and TICKET_ACK_ENABLED belong to mail but share no prefix
    # with GRAPH_*.
    GROUPS = {
        'llm': {'label': 'AI assistant',
                'blurb': 'Chat assistant and ticket summaries.'},
        'mail': {'label': 'Email intake',
                 'blurb': 'Turn mail sent to the team address into tickets.'},
        'aws': {'label': 'AWS Secrets Manager',
                'blurb': 'Central credentials used to list and read secrets across projects.'},
    }

    # Only keys listed here can be written through the API. An open key/value
    # store reachable by HTTP is a way to overwrite SECRET_KEY by accident.
    EDITABLE = {
        'GEMINI_API_KEY': {'group': 'llm', 'label': 'Gemini API key', 'secret': True,
                           'help': 'Enables the chat assistant and ticket summaries.'},
        'GEMINI_MODEL': {'group': 'llm', 'label': 'Gemini model', 'secret': False,
                         'help': 'Defaults to gemini-2.5-flash.'},
        'GRAPH_TENANT_ID': {'group': 'mail', 
            'label': 'Microsoft tenant ID', 'secret': False,
            'help': 'Entra ID → Overview → Directory (tenant) ID.'},
        'GRAPH_CLIENT_ID': {'group': 'mail', 
            'label': 'Microsoft client ID', 'secret': False,
            'help': 'The app registration\'s Application (client) ID.'},
        'GRAPH_CLIENT_SECRET': {'group': 'mail', 
            'label': 'Microsoft client secret', 'secret': True,
            'help': 'The secret VALUE, not its ID. Shown only once when created.'},
        'DEVOPS_MAILBOX': {'group': 'mail', 
            'label': 'Team mailbox', 'secret': False,
            'help': 'The shared mailbox to poll, e.g. devops@pacewisdom.com.'},
        'TICKET_TRIGGER_ADDRESS': {'group': 'mail', 
            'label': 'Trigger address', 'secret': False,
            'help': 'A ticket is created only when this appears in the email '
                    'body. Blank uses the mailbox address.'},
        'TICKET_ACK_ENABLED': {'group': 'mail',
            'label': 'Acknowledge senders', 'secret': False,
            'help': 'Set to 1 to email the sender a ticket reference. This mails '
                    'real people as your team — leave at 0 until the queue looks right.'},
        'AWS_ACCESS_KEY_ID': {'group': 'aws',
            'label': 'AWS access key ID', 'secret': False,
            'help': 'IAM user/role key with secretsmanager:ListSecrets and '
                    'GetSecretValue.'},
        'AWS_SECRET_ACCESS_KEY': {'group': 'aws',
            'label': 'AWS secret access key', 'secret': True,
            'help': 'The secret value paired with the access key ID above.'},
        'AWS_REGION': {'group': 'aws',
            'label': 'Default region', 'secret': False,
            'help': 'Default region for listing secrets; Secrets Manager is '
                    'regional. Defaults to us-east-1.'},
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


def setting_bool(key, default=False):
    """Resolve a setting as a boolean.

    Needed because a stored value is always a string, and '0' is truthy in
    Python — reading TICKET_ACK_ENABLED naively would start mailing people the
    moment someone typed 0 to switch it off.
    """
    value = get_setting(key, None)
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


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
