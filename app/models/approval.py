from datetime import datetime, timezone
from ..extensions import db


class Approval(db.Model):
    __tablename__ = 'approvals'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('environment_requests.id'),
                           nullable=False, unique=True)
    approver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    decision = db.Column(db.String(20), nullable=False)  # 'approved' or 'declined'
    comment = db.Column(db.Text, nullable=True)
    auto_approved = db.Column(db.Boolean, default=False)
    decided_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    approver = db.relationship('User', foreign_keys=[approver_id])

    def __repr__(self):
        return f'<Approval req={self.request_id} ({self.decision})>'
