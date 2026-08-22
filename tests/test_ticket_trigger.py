"""What counts as a request.

The address lives in the signature block of everyone who has ever mailed the
team, and in the quoted header of every reply. Matching naively would ticket
half the mailbox, so these cases are the feature.
"""
import pytest

from app.services.ticket_intake import body_matches, normalise_body, trigger_address

ADDR = 'devops@pacewisdom.com'


@pytest.fixture
def configured(app):
    app.config['TICKET_TRIGGER_ADDRESS'] = ADDR
    app.config['DEVOPS_MAILBOX'] = ADDR
    return app


def test_a_fresh_mention_matches(configured):
    assert body_matches('Hi, please loop in devops@pacewisdom.com on this.', ADDR)


def test_matching_is_case_insensitive(configured):
    assert body_matches('cc DevOps@PaceWisdom.COM please', ADDR)


def test_a_mention_only_in_a_signature_does_not_match(configured):
    body = ('Can someone restart the UAT box?\n'
            '--\n'
            'Priya\nPlatform Team | devops@pacewisdom.com\n')
    assert body_matches(body, ADDR) is False


def test_a_mention_only_in_a_quoted_reply_does_not_match(configured):
    body = ('Thanks, that worked.\n\n'
            '> Raised with devops@pacewisdom.com yesterday\n'
            '> and they fixed it.\n')
    assert body_matches(body, ADDR) is False


def test_a_mention_only_after_an_on_wrote_marker_does_not_match(configured):
    body = ('Agreed.\n\n'
            'On Tue, 12 Aug 2026 at 10:04, Priya <p@x.com> wrote:\n'
            'Please contact devops@pacewisdom.com about the outage.\n')
    assert body_matches(body, ADDR) is False


def test_a_mention_only_in_a_forwarded_header_block_does_not_match(configured):
    body = ('FYI\n\n'
            '-----Original Message-----\n'
            'From: devops@pacewisdom.com\n'
            'Subject: maintenance\n')
    assert body_matches(body, ADDR) is False


def test_no_mention_does_not_match(configured):
    assert body_matches('Please restart the UAT environment.', ADDR) is False


def test_an_empty_body_does_not_match(configured):
    assert body_matches('', ADDR) is False
    assert body_matches(None, ADDR) is False


def test_an_html_body_still_matches(configured):
    """Exchange may ignore our plain-text Prefer header."""
    body = '<html><body><p>Hi <a href="mailto:devops@pacewisdom.com">devops@pacewisdom.com</a></p></body></html>'
    assert body_matches(body, ADDR)


def test_an_empty_configured_address_never_matches(configured):
    assert body_matches('devops@pacewisdom.com', '') is False


def test_the_trigger_address_is_configurable(configured):
    configured.config['TICKET_TRIGGER_ADDRESS'] = 'support@other.com'
    assert trigger_address() == 'support@other.com'
    assert body_matches('mail devops@pacewisdom.com', trigger_address()) is False
    assert body_matches('mail support@other.com', trigger_address()) is True


def test_the_trigger_address_falls_back_to_the_mailbox(app):
    app.config['TICKET_TRIGGER_ADDRESS'] = None
    app.config['DEVOPS_MAILBOX'] = ADDR
    assert trigger_address() == ADDR


def test_normalise_collapses_and_casefolds(configured):
    assert normalise_body('  Hello   THERE\n\nworld ') == 'hello there world'
