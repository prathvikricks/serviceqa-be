"""The poller: idempotent, isolated per message, and silent when unconfigured.

No network. graph_mail's two I/O functions are monkeypatched at the module
attribute, which is only possible because I/O, model calls and orchestration
live in three separate modules.
"""
import pytest

from app.extensions import db
from app.models.email_intake import EmailIntakeMessage
from app.models.ticket import Ticket
from app.services import graph_mail, ticket_intake

from conftest import login

ADDR = 'devops@pacewisdom.com'


@pytest.fixture
def intake(app):
    app.config.update(GRAPH_TENANT_ID='t', GRAPH_CLIENT_ID='c',
                      GRAPH_CLIENT_SECRET='s', DEVOPS_MAILBOX=ADDR,
                      TICKET_TRIGGER_ADDRESS=ADDR, TICKET_ACK_ENABLED=False,
                      GEMINI_API_KEY=None)
    return app


def message(**over):
    msg = {
        'id': 'AAMk-1',
        'internetMessageId': '<abc@mail>',
        'conversationId': 'CONV-1',
        'subject': 'Need prod DB access',
        'receivedDateTime': '2026-08-22T09:00:00Z',
        'from': {'emailAddress': {'address': 'priya@example.com', 'name': 'Priya'}},
        'uniqueBody': {'content': f'Hi, please loop in {ADDR} — I need read access.'},
    }
    msg.update(over)
    return msg


def _feed(monkeypatch, *messages):
    calls = []

    def fake_fetch(since, limit=None):
        calls.append(since)
        return list(messages)

    monkeypatch.setattr(graph_mail, 'fetch_messages', fake_fetch)
    return calls


def test_a_triggering_message_creates_one_ticket(intake, monkeypatch):
    _feed(monkeypatch, message())
    counts = ticket_intake.poll_once()

    assert counts['created'] == 1
    ticket = Ticket.query.one()
    assert ticket.reference == 'DVO-000001'
    assert ticket.requester_email == 'priya@example.com'
    assert ticket.source == 'email'
    # No key in tests, so the raw subject is the title.
    assert ticket.title == 'Need prod DB access'
    assert ticket.enriched_by == 'fallback'
    assert EmailIntakeMessage.query.one().disposition == 'ticket_created'


def test_the_same_message_twice_still_makes_one_ticket(intake, monkeypatch):
    _feed(monkeypatch, message())
    ticket_intake.poll_once()
    ticket_intake.poll_once()

    assert Ticket.query.count() == 1


def test_a_reply_on_a_known_thread_creates_nothing(intake, monkeypatch):
    _feed(monkeypatch, message())
    ticket_intake.poll_once()

    _feed(monkeypatch, message(id='AAMk-2', conversationId='CONV-1'))
    counts = ticket_intake.poll_once()

    assert counts['created'] == 0
    assert Ticket.query.count() == 1
    assert EmailIntakeMessage.query.filter_by(disposition='duplicate').count() == 1


def test_a_message_without_the_trigger_is_recorded_not_ticketed(intake, monkeypatch):
    _feed(monkeypatch, message(uniqueBody={'content': 'Just an FYI, nothing needed.'}))
    counts = ticket_intake.poll_once()

    assert counts['created'] == 0
    assert Ticket.query.count() == 0
    row = EmailIntakeMessage.query.one()
    assert row.disposition == 'no_trigger'


def test_one_bad_message_does_not_abort_the_batch(intake, monkeypatch):
    """The isolation guarantee — the most important behaviour here."""
    good_a = message(id='A', conversationId='CA')
    bad = message(id='B', conversationId='CB')
    good_b = message(id='C', conversationId='CC')

    from app.services import ticket_agent

    def explode(subject, body, client=None):
        if 'boom' in (body or ''):
            raise RuntimeError('enrichment blew up')
        return {'title': subject, 'summary': None, 'category': None,
                'urgency': None, 'enriched_by': 'fallback'}

    bad['uniqueBody'] = {'content': f'boom {ADDR}'}
    monkeypatch.setattr(ticket_agent, 'enrich', explode)
    _feed(monkeypatch, good_a, bad, good_b)

    counts = ticket_intake.poll_once()

    assert counts['created'] == 2, 'the good messages either side must survive'
    assert counts['errors'] == 1
    assert EmailIntakeMessage.query.filter_by(disposition='error').count() == 1


def test_a_transient_fetch_failure_is_swallowed(intake, monkeypatch):
    def boom(since, limit=None):
        raise graph_mail.MailError('Graph 429: slow down')

    monkeypatch.setattr(graph_mail, 'fetch_messages', boom)
    assert ticket_intake.poll_once() == {'fetched': 0, 'created': 0,
                                         'skipped': 0, 'errors': 0}


def test_the_poller_makes_no_call_when_unconfigured(app, monkeypatch):
    app.config.update(GRAPH_TENANT_ID=None, GRAPH_CLIENT_ID=None,
                      GRAPH_CLIENT_SECRET=None, DEVOPS_MAILBOX=None)
    calls = _feed(monkeypatch, message())

    ticket_intake.poll_once()
    assert calls == [], 'must not reach Graph when the feature is off'


def test_the_watermark_rewinds_by_the_overlap(intake, monkeypatch):
    """Re-scanning a short window absorbs clock skew; dedup makes it free."""
    from datetime import timedelta

    intake.config['MAIL_INTAKE_OVERLAP_MINUTES'] = 10
    calls = _feed(monkeypatch, message())
    ticket_intake.poll_once()

    calls.clear()
    ticket_intake.poll_once()

    received = EmailIntakeMessage.query.one().received_at
    expected = received.replace(tzinfo=None) - timedelta(minutes=10)
    assert calls[0].replace(tzinfo=None) == expected


def test_the_acknowledgement_is_skipped_when_disabled(intake, monkeypatch):
    _feed(monkeypatch, message())
    ticket_intake.poll_once()
    assert Ticket.query.one().ack_state == 'disabled'


def test_a_failed_acknowledgement_keeps_the_ticket(intake, monkeypatch):
    intake.config['TICKET_ACK_ENABLED'] = True

    def boom(*a, **kw):
        raise graph_mail.MailError('Graph 403: Mail.Send not consented')

    monkeypatch.setattr(graph_mail, 'send_mail', boom)
    _feed(monkeypatch, message())
    ticket_intake.poll_once()

    ticket = Ticket.query.one()
    assert ticket.ack_state == 'failed'
    assert 'Mail.Send' in ticket.ack_error


def test_a_successful_acknowledgement_is_stamped_and_references_the_ticket(intake, monkeypatch):
    intake.config['TICKET_ACK_ENABLED'] = True
    sent = {}

    def capture(to, subject, body, in_reply_to=None):
        sent.update(to=to, subject=subject, body=body, in_reply_to=in_reply_to)

    monkeypatch.setattr(graph_mail, 'send_mail', capture)
    _feed(monkeypatch, message())
    ticket_intake.poll_once()

    ticket = Ticket.query.one()
    assert ticket.ack_state == 'sent' and ticket.ack_sent_at is not None
    assert sent['to'] == 'priya@example.com'
    assert 'DVO-000001' in sent['subject']
    # Threads under the sender's original rather than starting a new chain.
    assert sent['in_reply_to'] == '<abc@mail>'


def test_mail_from_the_mailbox_itself_is_never_acknowledged(intake, monkeypatch):
    """Otherwise the ack is re-polled and the team mails itself in a loop."""
    intake.config['TICKET_ACK_ENABLED'] = True
    monkeypatch.setattr(graph_mail, 'send_mail',
                        lambda *a, **kw: pytest.fail('must not send'))
    _feed(monkeypatch, message(**{'from': {'emailAddress': {'address': ADDR, 'name': 'DevOps'}}}))
    ticket_intake.poll_once()

    assert Ticket.query.one().ack_state == 'disabled'


def test_a_known_sender_is_linked_to_their_user(intake, monkeypatch, users):
    _feed(monkeypatch, message(
        **{'from': {'emailAddress': {'address': users['dev'].email, 'name': 'Dev'}}}))
    ticket_intake.poll_once()

    assert Ticket.query.one().requester_user_id == users['dev'].id


def test_the_scheduler_wrapper_never_raises(intake, monkeypatch):
    """APScheduler swallowing an exception would stop the queue filling silently."""
    from app.services import scheduler_service

    def boom():
        raise RuntimeError('everything is on fire')

    monkeypatch.setattr(ticket_intake, 'poll_once', boom)
    scheduler_service.poll_devops_mailbox(intake)   # must not raise


def test_the_poller_is_registered_with_the_scheduler():
    import inspect

    from app.services import scheduler_service

    source = inspect.getsource(scheduler_service.init_scheduler)
    assert "id='mail_intake'" in source


def test_the_connection_test_reports_a_graph_error_verbatim(intake, monkeypatch, client, users):
    """On setup day the specific message is the whole value."""
    def boom(since, limit=None):
        raise graph_mail.MailPermanentError(
            'Graph 403: Access to OData is disabled / policy does not cover mailbox')

    monkeypatch.setattr(graph_mail, 'fetch_messages', boom)

    login(client, 'admin')
    resp = client.post('/api/v1/tickets/intake/test')
    assert resp.status_code == 502
    assert 'policy does not cover mailbox' in resp.get_json()['error']


def test_the_connection_test_says_when_nothing_is_configured(app, client, users):
    app.config.update(GRAPH_TENANT_ID=None, GRAPH_CLIENT_ID=None,
                      GRAPH_CLIENT_SECRET=None, DEVOPS_MAILBOX=None)
    login(client, 'admin')
    resp = client.post('/api/v1/tickets/intake/test')
    assert resp.status_code == 503
    assert 'not configured' in resp.get_json()['error']


def test_the_connection_test_succeeds_when_the_mailbox_answers(intake, monkeypatch,
                                                               client, users):
    _feed(monkeypatch, message())
    login(client, 'admin')
    body = client.post('/api/v1/tickets/intake/test').get_json()
    assert body['reachable'] is True
    assert body['mailbox'] == ADDR


def test_only_an_admin_can_trigger_intake(intake, monkeypatch, client, users):
    _feed(monkeypatch, message())
    for username, expected in (('dev', 403), ('ops', 403), ('admin', 200)):
        client.post('/api/v1/auth/logout')
        login(client, username)
        assert client.post('/api/v1/tickets/intake/run').status_code == expected


def test_a_manual_run_creates_tickets_immediately(intake, monkeypatch, client, users):
    _feed(monkeypatch, message())
    login(client, 'admin')
    body = client.post('/api/v1/tickets/intake/run').get_json()
    assert body['created'] == 1
