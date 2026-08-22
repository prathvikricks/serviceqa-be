"""Ticket persistence, and the constraint that makes intake idempotent."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.email_intake import EmailIntakeMessage
from app.models.ticket import Ticket, TicketComment

from conftest import make_user


def _ticket(**over):
    data = dict(title='Need prod DB access', body='Hi team, please grant access.',
                requester_email='dev@example.com')
    data.update(over)
    t = Ticket(**data)
    db.session.add(t)
    db.session.commit()
    return t


def test_a_ticket_defaults_to_open_email_source(app):
    t = _ticket()
    assert t.status == 'open'
    assert t.source == 'email'
    assert t.is_open is True


def test_reference_is_stamped_from_the_id(app):
    t = Ticket(title='x', body='y')
    db.session.add(t)
    db.session.flush()
    assert t.stamp_reference() == f'DVO-{t.id:06d}'


def test_terminal_statuses_are_not_open(app):
    assert _ticket(status='resolved').is_open is False
    assert _ticket(status='closed').is_open is False


def test_requester_label_falls_back(app):
    assert _ticket(requester_name='Ada').requester_label == 'Ada'
    assert _ticket(requester_name=None).requester_label == 'dev@example.com'
    assert _ticket(requester_name=None, requester_email=None).requester_label == 'Unknown'


def test_the_same_graph_message_cannot_ticket_twice(app):
    """The unique index is the real guarantee that intake is idempotent."""
    _ticket(graph_message_id='AAMk-1')
    with pytest.raises(IntegrityError):
        _ticket(graph_message_id='AAMk-1')
    db.session.rollback()


def test_the_intake_ledger_also_refuses_duplicates(app):
    def row():
        from datetime import datetime
        m = EmailIntakeMessage(graph_message_id='AAMk-9', received_at=datetime.now(),
                               disposition='no_trigger')
        db.session.add(m)
        db.session.commit()

    row()
    with pytest.raises(IntegrityError):
        row()
    db.session.rollback()


def test_deleting_a_ticket_deletes_its_comments(app, users):
    t = _ticket()
    db.session.add(TicketComment(ticket_id=t.id, author_id=users['admin'].id,
                                 body='Looking into it'))
    db.session.commit()
    assert t.comments.count() == 1

    db.session.delete(t)
    db.session.commit()
    assert TicketComment.query.count() == 0


def test_assignee_and_requester_are_distinct_user_links(app, users):
    """Two FKs to users on one table — both need explicit foreign_keys."""
    requester = make_user('asker', 'developer')
    t = _ticket(assignee_id=users['devops'].id, requester_user_id=requester.id)
    db.session.expire_all()
    t = db.session.get(Ticket, t.id)
    assert t.assignee.username == 'ops'
    assert t.requester_user.username == 'asker'
