"""DevOps tickets.

A third intake path alongside the two structured request types. Mail sent to the
team mailbox becomes a ticket, so work that arrives as prose ("can someone give
me access to…") is tracked in the same place as work that arrives as a form.

Deliberately NOT a third `EnvironmentRequest.request_type`: a ticket has a
different lifecycle (open/in_progress/resolved rather than
pending/approved/declined), an assignee rather than an approver, and none of the
schedule, cloud-service or cost machinery a request carries. It is also global —
tickets must never leak into the project-scoped approvals inbox.
"""
from datetime import datetime, timezone
from ..extensions import db


class Ticket(db.Model):
    __tablename__ = 'tickets'

    STATUSES = ['open', 'in_progress', 'resolved', 'closed']
    CATEGORIES = ['access', 'incident', 'provisioning', 'question', 'other']
    URGENCIES = ['low', 'normal', 'high']
    SOURCES = ['email', 'manual']
    ACK_STATES = ['pending', 'sent', 'failed', 'disabled']

    # Statuses that mean "nobody is going to look at this again".
    TERMINAL_STATUSES = ('resolved', 'closed')

    id = db.Column(db.Integer, primary_key=True)
    # Human-quotable handle ('DVO-000123'). Stamped after flush, since it needs
    # the id. Goes in the acknowledgement subject so replies can be traced.
    reference = db.Column(db.String(20), nullable=True, unique=True, index=True)
    title = db.Column(db.String(300), nullable=False)
    # The full original message. Kept verbatim — the LLM summary is a
    # convenience, and a triager must always be able to read what was actually
    # sent.
    body = db.Column(db.Text, nullable=False)
    summary = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(20), nullable=True)
    urgency = db.Column(db.String(10), nullable=True)

    status = db.Column(db.String(20), default='open', nullable=False)
    assignee_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    # Optional: an email rarely says which project it is about, so this is set
    # during triage rather than at intake.
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)

    requester_email = db.Column(db.String(255), nullable=True)
    requester_name = db.Column(db.String(255), nullable=True)
    # Set when the sender's address matches a known user, so they can read their
    # own ticket without being DevOps.
    requester_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    source = db.Column(db.String(20), default='email', nullable=False)
    # 'gemini' or 'fallback' — shown in the UI so a poor summary explains itself
    # rather than looking like something a person wrote.
    enriched_by = db.Column(db.String(20), default='fallback', nullable=False)

    # --- Mail provenance / de-duplication -----------------------------------
    # The poller is at-least-once: it may crash mid-batch and re-read the same
    # messages. This unique column, not the mailbox read-flag, is what actually
    # guarantees one ticket per message.
    graph_message_id = db.Column(db.String(512), nullable=True, unique=True, index=True)
    # Replies land on the same thread. Without this a follow-up that repeats the
    # trigger address would open a second ticket for the same conversation.
    graph_conversation_id = db.Column(db.String(512), nullable=True, index=True)

    # The acknowledgement is best-effort: a send failure must never cost us the
    # ticket, so the outcome is recorded rather than raised.
    ack_state = db.Column(db.String(20), default='pending', nullable=False)
    ack_sent_at = db.Column(db.DateTime, nullable=True)
    ack_error = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)

    assignee = db.relationship('User', foreign_keys=[assignee_id])
    requester_user = db.relationship('User', foreign_keys=[requester_user_id])
    project = db.relationship('Project', foreign_keys=[project_id])
    comments = db.relationship('TicketComment', backref='ticket', lazy='dynamic',
                               cascade='all, delete-orphan',
                               order_by='TicketComment.id')

    @property
    def is_open(self):
        return self.status not in self.TERMINAL_STATUSES

    @property
    def requester_label(self):
        """Best available name for whoever asked, for lists and headers."""
        return self.requester_name or self.requester_email or 'Unknown'

    def stamp_reference(self):
        """Set the human-facing reference. Requires a flushed id."""
        self.reference = f'DVO-{self.id:06d}'
        return self.reference

    def __repr__(self):
        return f'<Ticket {self.reference or self.id} ({self.status})>'


class TicketComment(db.Model):
    __tablename__ = 'ticket_comments'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=False)
    # Null for system notes, which have no human author.
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    body = db.Column(db.Text, nullable=False)
    # System notes ('status open -> in_progress') share the thread with human
    # comments so the detail page has one timeline instead of two.
    is_system = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    author = db.relationship('User', foreign_keys=[author_id])

    def __repr__(self):
        return f'<TicketComment ticket={self.ticket_id}>'
