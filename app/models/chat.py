"""Chat intake conversations.

A developer describes what they need; the agent asks follow-ups and eventually
proposes a request draft. The transcript is kept rather than discarded: this app
already treats auditability as first-class, and an approver looking at a request
is better served by the original ask than by the tidied-up form fields.
"""
from datetime import datetime, timezone
from ..extensions import db


class ChatConversation(db.Model):
    __tablename__ = 'chat_conversations'

    # Turns are capped so a wandering conversation cannot run up unbounded
    # token spend. Past this the developer is pointed at the forms.
    MAX_TURNS = 20

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    status = db.Column(db.String(20), default='open', nullable=False)  # open | closed
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship('User', foreign_keys=[user_id])
    project = db.relationship('Project', foreign_keys=[project_id])
    messages = db.relationship('ChatMessage', backref='conversation',
                               lazy='dynamic', cascade='all, delete-orphan',
                               order_by='ChatMessage.id')

    @property
    def turn_count(self):
        """User turns so far — what MAX_TURNS is measured against."""
        return self.messages.filter_by(role='user').count()

    def __repr__(self):
        return f'<ChatConversation #{self.id} project={self.project_id}>'


class ChatMessage(db.Model):
    __tablename__ = 'chat_messages'

    ROLES = ['user', 'agent']

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('chat_conversations.id'),
                                nullable=False)
    role = db.Column(db.String(10), nullable=False)
    content = db.Column(db.Text, nullable=False)
    # The validated draft this turn produced, if any. Stored so the transcript
    # explains itself without re-running the model.
    draft = db.Column(db.JSON, nullable=True)
    request_type = db.Column(db.String(20), nullable=True)  # 'service' | 'repo'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f'<ChatMessage {self.role} convo={self.conversation_id}>'
