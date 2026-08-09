from datetime import datetime, timezone

from ..extensions import db


class ProjectSecret(db.Model):
    """A named credential belonging to a project.

    Scope: project-wide when ``environment_id`` is NULL, otherwise pinned to one
    environment — so ``API_URL`` can hold a different value for dev and for UAT
    without renaming the key.

    The value is Fernet-encrypted at rest using the same key as cloud
    credentials (``services/crypto.py``, ``CRED_KEY``).
    """
    __tablename__ = 'project_secrets'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False,
                           index=True)
    # NULL = applies to the whole project.
    environment_id = db.Column(db.Integer, db.ForeignKey('environments.id'), nullable=True)
    key = db.Column(db.String(100), nullable=False)
    # Ciphertext. Never read this column directly — use get_value()/set_value().
    value = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    environment = db.relationship('Environment')
    creator = db.relationship('User', foreign_keys=[created_by])

    __table_args__ = (
        # NOTE: in SQL, NULLs don't compare equal, so this constraint does not
        # stop two project-wide secrets sharing a key. The create/update
        # endpoints check for that explicitly.
        db.UniqueConstraint('project_id', 'environment_id', 'key',
                            name='uq_project_env_secret_key'),
    )

    # --- the single crypto boundary ---------------------------------------
    # Everything in and out goes through these two. A raw read of `.value`
    # yields ciphertext; a raw write would silently store a plaintext secret.

    def get_value(self):
        from ..services.crypto import decrypt
        try:
            return decrypt(self.value)
        except Exception:
            # Wrong/rotated CRED_KEY. Return empty rather than 500 so the rest
            # of the list still renders and the problem is visible in the UI.
            return ''

    def set_value(self, plaintext):
        from ..services.crypto import encrypt
        self.value = encrypt(plaintext or '')

    @property
    def scope_label(self):
        return self.environment.display_name if self.environment else 'All environments'

    def __repr__(self):
        return f'<ProjectSecret {self.key} (project={self.project_id})>'
