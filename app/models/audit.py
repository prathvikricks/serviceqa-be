from datetime import datetime, timezone
from ..extensions import db


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.JSON, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', foreign_keys=[user_id])

    ACTIONS = [
        'project_created', 'project_updated',
        'environment_created', 'environment_updated',
        'service_created', 'service_updated',
        'member_added', 'member_removed', 'member_permission_updated',
        'secret_created', 'secret_updated', 'secret_deleted', 'secret_revealed',
        'request_created', 'request_cancelled',
        'request_approved', 'request_declined',
        'env_starting', 'env_started', 'env_start_failed',
        'env_stopping', 'env_stopped', 'env_stop_failed',
        'emergency_stop',
        'user_created', 'user_updated', 'user_login',
        'password_reset_requested', 'password_reset', 'password_changed',
        'email_verified', 'profile_updated',
        'invite_created', 'invite_resent', 'invite_revoked', 'invite_accepted',
    ]

    @staticmethod
    def log(action, entity_type, entity_id=None, user_id=None, details=None,
            ip_address=None):
        entry = AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            ip_address=ip_address,
        )
        db.session.add(entry)
        db.session.commit()
        return entry

    def __repr__(self):
        return f'<AuditLog {self.action} {self.entity_type}#{self.entity_id}>'
