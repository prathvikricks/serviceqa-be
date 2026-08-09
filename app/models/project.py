import re
from datetime import datetime, timezone
from ..extensions import db


class Project(db.Model):
    __tablename__ = 'projects'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    cloud_provider = db.Column(db.String(20), nullable=False)  # 'azure' or 'aws'
    # 'mock' = simulated cloud, no SDK calls and no spend (the default, so the
    # app is fully demoable with no credentials); 'real' = uses provider_config.
    mode = db.Column(db.String(10), default='mock', nullable=False)
    # Provider credentials + settings. Secret fields are Fernet-encrypted at
    # rest — always go through get/set_provider_config, never touch the column.
    provider_config = db.Column(db.JSON, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('slug', name='uq_project_slug'),
    )

    PROVIDERS = [('aws', 'Amazon Web Services'), ('azure', 'Microsoft Azure')]
    MODES = [('mock', 'Mock (simulated, no cost)'), ('real', 'Real (uses credentials)')]

    # Which provider_config keys hold secrets. These are stored encrypted; the
    # rest of the blob (region, subscription id, …) stays readable.
    SECRET_KEYS = ('secret_access_key', 'client_secret')

    # Relationships
    creator = db.relationship('User', foreign_keys=[created_by])
    environments = db.relationship('Environment', backref='project', lazy='dynamic',
                                   cascade='all, delete-orphan')
    members = db.relationship('ProjectMember', backref='project', lazy='dynamic',
                              cascade='all, delete-orphan')
    secrets = db.relationship('ProjectSecret', backref='project', lazy='dynamic',
                              cascade='all, delete-orphan')

    # --- provider_config: the single crypto boundary -----------------------
    # Secret values are held as {'_enc': <fernet token>} inside the JSON blob.
    # Read/write ONLY through these two methods; a raw read of the column gives
    # you ciphertext, and a raw write silently stores a credential in plaintext.

    def get_provider_config(self):
        from ..services.crypto import decrypt
        cfg = dict(self.provider_config or {})
        for key in self.SECRET_KEYS:
            val = cfg.get(key)
            if isinstance(val, dict) and '_enc' in val:
                try:
                    cfg[key] = decrypt(val['_enc'])
                except Exception:
                    cfg[key] = ''
        return cfg

    def set_provider_config(self, cfg):
        from ..services.crypto import encrypt
        out = dict(cfg or {})
        for key in self.SECRET_KEYS:
            val = out.get(key)
            if val:
                out[key] = {'_enc': encrypt(val)}
            elif key in out:
                out.pop(key)
        self.provider_config = out

    def has_secret(self, key):
        """True if a secret field is set, without decrypting it."""
        return bool((self.provider_config or {}).get(key))

    @staticmethod
    def generate_slug(name):
        slug = re.sub(r'[^\w\s-]', '', name.lower())
        slug = re.sub(r'[\s_]+', '-', slug)
        return re.sub(r'-+', '-', slug).strip('-')

    def __repr__(self):
        return f'<Project {self.name}>'
