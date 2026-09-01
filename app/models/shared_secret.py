from datetime import datetime, timezone

from ..extensions import db


class SharedSecret(db.Model):
    """A secret defined once in a central catalog and attached to many projects.

    Unlike ProjectSecret (which belongs to exactly one project), a SharedSecret
    is the single source of truth for its value: editing it here changes it for
    every project it's attached to. The value is Fernet-encrypted at rest using
    the same key as project secrets and cloud credentials (``services/crypto.py``,
    ``CRED_KEY``).
    """
    __tablename__ = 'shared_secrets'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False)
    # Ciphertext. Never read this column directly — use get_value()/set_value().
    value = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    creator = db.relationship('User', foreign_keys=[created_by])
    attachments = db.relationship('SharedSecretAttachment', backref='shared_secret',
                                  lazy='dynamic', cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('key', name='uq_shared_secret_key'),
    )

    # --- the single crypto boundary (mirrors ProjectSecret) ----------------

    def get_value(self):
        from ..services.crypto import decrypt
        try:
            return decrypt(self.value)
        except Exception:
            # Wrong/rotated CRED_KEY — empty rather than 500, same as ProjectSecret.
            return ''

    def set_value(self, plaintext):
        from ..services.crypto import encrypt
        self.value = encrypt(plaintext or '')

    def __repr__(self):
        return f'<SharedSecret {self.key}>'


class SharedSecretAttachment(db.Model):
    """Links a SharedSecret to a project, optionally pinned to one environment.

    NULL ``environment_id`` = the shared secret applies to the whole project.
    The unique constraint stops the same secret being attached to the same
    (project, environment) scope twice.
    """
    __tablename__ = 'shared_secret_attachments'

    id = db.Column(db.Integer, primary_key=True)
    shared_secret_id = db.Column(db.Integer, db.ForeignKey('shared_secrets.id'),
                                 nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False,
                           index=True)
    environment_id = db.Column(db.Integer, db.ForeignKey('environments.id'), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project = db.relationship('Project')
    environment = db.relationship('Environment')
    creator = db.relationship('User', foreign_keys=[created_by])

    __table_args__ = (
        db.UniqueConstraint('shared_secret_id', 'project_id', 'environment_id',
                            name='uq_shared_secret_attachment'),
    )

    @property
    def scope_label(self):
        return self.environment.display_name if self.environment else 'All environments'

    def __repr__(self):
        return f'<SharedSecretAttachment secret={self.shared_secret_id} project={self.project_id}>'
