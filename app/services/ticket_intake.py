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
    cfg = current_app.config
    return (cfg.get('TICKET_TRIGGER_ADDRESS')
            or cfg.get('DEVOPS_MAILBOX') or '').strip()


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
