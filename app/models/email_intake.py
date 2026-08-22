"""Ledger of every mailbox message the poller has looked at.

This table, not the mailbox, is the record of what has been handled.

The obvious alternative — flipping the message's `isRead` flag through Graph —
was rejected twice over. It needs `Mail.ReadWrite`, widening a read-only grant
into write access over a mailbox purely for bookkeeping; and the mailbox is read
by humans in Outlook, so any message someone opens before the poller does would
be silently skipped.

Keeping a local ledger also records *why nothing happened*. When someone asks
"why didn't my email become a ticket", the row with disposition='no_trigger' is
the answer, which neither a read flag nor a folder move could ever give.
"""
from datetime import datetime, timezone
from ..extensions import db


class EmailIntakeMessage(db.Model):
    __tablename__ = 'email_intake_messages'

    DISPOSITIONS = ['ticket_created', 'no_trigger', 'duplicate', 'error']

    id = db.Column(db.Integer, primary_key=True)
    # Graph's message id. Unique: this constraint, not any remote state, is what
    # guarantees one ticket per message when the poller retries a batch.
    graph_message_id = db.Column(db.String(512), nullable=False, unique=True, index=True)
    internet_message_id = db.Column(db.String(512), nullable=True)
    # Drives the watermark for the next fetch window.
    received_at = db.Column(db.DateTime, nullable=False, index=True)
    subject = db.Column(db.String(998), nullable=True)   # RFC 5322 line limit
    from_email = db.Column(db.String(255), nullable=True)

    disposition = db.Column(db.String(20), nullable=False, index=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=True)
    error = db.Column(db.Text, nullable=True)
    processed_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    ticket = db.relationship('Ticket', foreign_keys=[ticket_id])

    def __repr__(self):
        return f'<EmailIntakeMessage {self.disposition} {self.graph_message_id[:20]}>'
