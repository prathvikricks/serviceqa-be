"""Turning inbound mail into tickets.

Split deliberately from graph_mail (pure provider I/O) and ticket_agent (pure
model call) so the interesting logic here — does this message deserve a ticket,
and have we already made one — is testable with no network and no credentials.

The trigger is body-text based: a ticket is created only when the configured
address appears in the *new* content of the message. That is narrower than
"anything in the mailbox", and the narrowing is the point — the mailbox is read
by humans and receives plenty of mail that is not a request.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from flask import current_app

logger = logging.getLogger(__name__)

# Everything from these markers onward is quoted history, not new content.
# Graph's `uniqueBody` already strips most of it server-side; these are the
# belt-and-braces for clients whose format Microsoft's heuristic misses.
_ON_WROTE = re.compile(
    r'(?mi)^\s*On .{5,120}\bwrote:\s*$'
    r'|^\s*-{2,}\s*Original Message\s*-{2,}\s*$'
    r'|^\s*_{5,}\s*$'
)
# RFC 3676 signature delimiter: a line containing exactly "-- " (or "--").
# This is the realistic false positive — the team address sits in the company
# signature block of everyone who has ever mailed the team.
_SIG_SPLIT = re.compile(r'(?m)^--\s*$')
_QUOTE_LINE = re.compile(r'(?m)^\s*>.*$')
# Forwarded-header blocks that survived the cut above.
_HEADER_BLOCK = re.compile(r'(?mi)^\s*(from|sent|to|cc|subject|date)\s*:.*$')
# Crude tag strip, for the case where Exchange ignores our plain-text Prefer
# header and hands back HTML anyway.
_TAG = re.compile(r'<[^>]+>')


def trigger_address():
    """The address whose presence in a body creates a ticket."""
    from ..models.setting import get_setting
    return (get_setting('TICKET_TRIGGER_ADDRESS')
            or get_setting('DEVOPS_MAILBOX') or '').strip()


def normalise_body(text):
    """Reduce a raw body to just the new prose the sender wrote.

    Order matters: cut the quoted thread first, then the signature, then any
    stragglers. Each step only ever removes text, so a false negative is the
    worst outcome — a mention that survives all of this is a real one.
    """
    if not text:
        return ''

    if '<' in text and '>' in text:
        # Only meaningful when we were handed HTML; harmless on plain text that
        # happens to contain angle brackets, since it strips nothing that looks
        # like an address.
        text = _TAG.sub(' ', text)

    for pattern in (_ON_WROTE, _SIG_SPLIT):
        match = pattern.search(text)
        if match:
            text = text[:match.start()]

    text = _QUOTE_LINE.sub('', text)
    text = _HEADER_BLOCK.sub('', text)
    return re.sub(r'\s+', ' ', text).casefold().strip()


def body_matches(text, address):
    """True if `address` appears in the sender's own new content.

    Substring rather than word-boundary matching: an address contains '@' and
    '.', both of which break \\b semantics, and people legitimately write
    "please loop in devops@example.com on this" mid-sentence.
    """
    address = (address or '').strip().casefold()
    if not address:
        return False
    return address in normalise_body(text)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _watermark():
    """Oldest receivedDateTime worth fetching again.

    Re-scanning a short overlap absorbs clock skew and out-of-order delivery.
    Re-seeing a message is free — the ledger's unique index turns it into a
    cheap SELECT, not a duplicate ticket.
    """
    from sqlalchemy import func
    from ..extensions import db
    from ..models.email_intake import EmailIntakeMessage
    from . import graph_mail

    newest = db.session.query(func.max(EmailIntakeMessage.received_at)).scalar()
    if newest is None:
        return graph_mail.default_since()

    overlap = current_app.config.get('MAIL_INTAKE_OVERLAP_MINUTES') or 10
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return newest - timedelta(minutes=overlap)


def _parse_received(value):
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return datetime.now(timezone.utc)


def _record(graph_id, msg, disposition, ticket_id=None, error=None):
    from ..extensions import db
    from ..models.email_intake import EmailIntakeMessage

    db.session.add(EmailIntakeMessage(
        graph_message_id=graph_id,
        internet_message_id=msg.get('internetMessageId'),
        received_at=_parse_received(msg.get('receivedDateTime')),
        subject=(msg.get('subject') or '')[:998] or None,
        from_email=_sender(msg)[0],
        disposition=disposition,
        ticket_id=ticket_id,
        error=(error or None),
    ))
    db.session.commit()


def _sender(msg):
    address = ((msg.get('from') or {}).get('emailAddress') or {})
    return (address.get('address') or None), (address.get('name') or None)


def _body_of(msg):
    """Prefer uniqueBody — Graph has already stripped the quoted thread."""
    for key in ('uniqueBody', 'body'):
        content = (msg.get(key) or {}).get('content')
        if content:
            return content
    return msg.get('bodyPreview') or ''


def process_message(msg):
    """One Graph message becomes at most one ticket. Idempotent on its id."""
    from sqlalchemy.exc import IntegrityError
    from ..extensions import db
    from ..models.audit import AuditLog
    from ..models.email_intake import EmailIntakeMessage
    from ..models.ticket import Ticket
    from ..models.user import User
    from . import ticket_agent

    graph_id = msg.get('id')
    if not graph_id:
        return None

    seen = EmailIntakeMessage.query.filter_by(graph_message_id=graph_id).first()
    if seen is not None:
        return None

    body = _body_of(msg)
    if not body_matches(body, trigger_address()):
        _record(graph_id, msg, 'no_trigger')
        return None

    conversation_id = msg.get('conversationId')
    if conversation_id:
        # A reply that repeats the address would otherwise open a second ticket
        # for a conversation already being worked.
        existing = Ticket.query.filter_by(graph_conversation_id=conversation_id).first()
        if existing is not None:
            _record(graph_id, msg, 'duplicate', ticket_id=existing.id)
            return None

    subject = msg.get('subject') or ''
    enriched = ticket_agent.enrich(subject, body)

    from_email, from_name = _sender(msg)
    known = User.query.filter_by(email=from_email).first() if from_email else None

    ticket = Ticket(
        title=enriched['title'],
        body=body,
        summary=enriched['summary'],
        category=enriched['category'],
        urgency=enriched['urgency'],
        enriched_by=enriched['enriched_by'],
        requester_email=from_email,
        requester_name=from_name,
        requester_user_id=known.id if known else None,
        source='email',
        graph_message_id=graph_id,
        graph_conversation_id=conversation_id,
    )
    db.session.add(ticket)
    try:
        db.session.flush()
        ticket.stamp_reference()
        db.session.commit()
    except IntegrityError:
        # Lost a race, or a retry got here first. The unique index is what makes
        # this safe rather than a check-then-insert.
        db.session.rollback()
        _record(graph_id, msg, 'duplicate')
        return None

    _record(graph_id, msg, 'ticket_created', ticket_id=ticket.id)
    AuditLog.log('ticket_created', 'ticket', ticket.id,
                 details={'source': 'email', 'from': from_email,
                          'reference': ticket.reference,
                          'enriched_by': ticket.enriched_by})

    send_acknowledgement(ticket, msg.get('internetMessageId'))
    return ticket


def send_acknowledgement(ticket, internet_message_id=None):
    """Best effort, and always after the ticket is committed.

    A send failure is recorded on the ticket rather than raised: the mail has
    already been received and the ticket must survive regardless. There is no
    automatic retry — a permanently misconfigured Mail.Send would otherwise log
    a failure per ticket per poll, forever.
    """
    from ..extensions import db
    from ..models.audit import AuditLog
    from . import graph_mail

    from ..models.setting import get_setting, setting_bool

    mailbox = (get_setting('DEVOPS_MAILBOX') or '').casefold()
    if (not setting_bool('TICKET_ACK_ENABLED')
            or not graph_mail.is_enabled()
            or not ticket.requester_email
            or ticket.requester_email.casefold() == mailbox
            or ticket.ack_state != 'pending'):
        ticket.ack_state = 'disabled'
        db.session.commit()
        return

    body = (
        f'Thanks — this has been logged as ticket {ticket.reference}.\n\n'
        f'  Title:  {ticket.title}\n'
        f'  Status: {ticket.status}\n\n'
        'The DevOps team will pick this up from the shared queue.\n\n'
        '— envmanager\n'
    )
    try:
        graph_mail.send_mail(
            ticket.requester_email,
            f'[{ticket.reference}] Re: {ticket.title[:120]}',
            body,
            in_reply_to=internet_message_id,
        )
        ticket.ack_state = 'sent'
        ticket.ack_sent_at = datetime.now(timezone.utc)
        ticket.ack_error = None
        db.session.commit()
        AuditLog.log('ticket_ack_sent', 'ticket', ticket.id)
    except Exception as exc:
        logger.warning('Acknowledgement failed for %s: %s', ticket.reference, exc)
        ticket.ack_state = 'failed'
        ticket.ack_error = str(exc)[:500]
        db.session.commit()
        AuditLog.log('ticket_ack_failed', 'ticket', ticket.id,
                     details={'error': str(exc)[:300]})


def check_connection():
    """Reach the mailbox once and report what happened, in words.

    poll_once deliberately swallows failures — a scheduled job that raises every
    two minutes is just log noise. But on setup day the specific Graph error is
    the whole point, so this surfaces it instead.
    """
    from . import graph_mail

    if not graph_mail.is_enabled():
        raise graph_mail.MailUnavailable(
            'Microsoft Graph is not configured: set the tenant id, client id, '
            'client secret and mailbox.')

    messages = graph_mail.fetch_messages(_watermark(), limit=1)
    return {'mailbox': current_app.config.get('DEVOPS_MAILBOX'),
            'reachable': True,
            'sample_fetched': len(messages)}


def poll_once():
    """Fetch and process one batch. Returns counts; never raises."""
    from ..extensions import db
    from . import graph_mail

    counts = {'fetched': 0, 'created': 0, 'skipped': 0, 'errors': 0}
    if not graph_mail.is_enabled():
        return counts

    try:
        messages = graph_mail.fetch_messages(_watermark())
    except graph_mail.MailUnavailable:
        return counts
    except graph_mail.MailError as exc:
        logger.warning('Mailbox poll failed: %s', exc)
        return counts

    counts['fetched'] = len(messages)
    for msg in messages:
        # Per-message isolation: one malformed email must not abort the batch.
        try:
            if process_message(msg) is not None:
                counts['created'] += 1
            else:
                counts['skipped'] += 1
        except Exception as exc:
            counts['errors'] += 1
            # The rollback is not optional — a poisoned session would fail every
            # remaining message in the batch too.
            db.session.rollback()
            logger.exception('Could not process message %s', msg.get('id'))
            try:
                _record(msg.get('id') or 'unknown', msg, 'error', error=str(exc)[:500])
            except Exception:
                db.session.rollback()

    if counts['created'] or counts['errors']:
        logger.info('Mailbox poll: %s', counts)
    return counts
