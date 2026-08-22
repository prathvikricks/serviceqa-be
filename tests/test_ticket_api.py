"""The ticket queue over HTTP: who can see it, and what triage accepts."""
import json

import pytest

from app.extensions import db
from app.models.ticket import Ticket

from conftest import login, make_user


@pytest.fixture
def ticket(app, users):
    t = Ticket(title='Prod DB access for Priya', body='Please grant read access.',
               requester_email='priya@example.com', source='email')
    db.session.add(t)
    db.session.flush()
    t.stamp_reference()
    db.session.commit()
    return t


def test_a_developer_cannot_see_the_queue(client, users, ticket):
    login(client, 'dev')
    assert client.get('/api/v1/tickets').status_code == 403


def test_devops_and_admin_can_see_the_queue(client, users, ticket):
    for username in ('ops', 'admin'):
        client.post('/api/v1/auth/logout')
        login(client, username)
        body = client.get('/api/v1/tickets').get_json()
        assert [t['reference'] for t in body['tickets']] == ['DVO-000001']
        assert body['counts']['open'] == 1


def test_the_queue_requires_authentication(client, ticket):
    assert client.get('/api/v1/tickets').status_code == 401


def test_a_manual_ticket_can_be_raised(client, users):
    login(client, 'ops')
    resp = client.post('/api/v1/tickets', json={'title': 'Rotate the staging cert'})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body['source'] == 'manual'
    assert body['reference'] == 'DVO-000001'
    # A manual ticket has nobody to acknowledge.
    assert body['ack_state'] == 'disabled'


def test_a_manual_ticket_needs_a_title(client, users):
    login(client, 'ops')
    assert client.post('/api/v1/tickets', json={'title': '  '}).status_code == 400


def test_status_moves_and_stamps_resolved_at(client, users, ticket):
    login(client, 'ops')
    for status in ('in_progress', 'resolved'):
        resp = client.patch(f'/api/v1/tickets/{ticket.id}', json={'status': status})
        assert resp.status_code == 200
    assert resp.get_json()['resolved_at'] is not None

    # Reopening clears it again — a resolved date on an open ticket is a lie.
    resp = client.patch(f'/api/v1/tickets/{ticket.id}', json={'status': 'open'})
    assert resp.get_json()['resolved_at'] is None


def test_an_unknown_status_is_rejected(client, users, ticket):
    login(client, 'ops')
    assert client.patch(f'/api/v1/tickets/{ticket.id}',
                        json={'status': 'wharrgarbl'}).status_code == 400


def test_a_ticket_cannot_be_assigned_to_a_developer(client, users, ticket):
    """DevOps work assigned to someone who cannot act looks handled but isn't."""
    login(client, 'ops')
    resp = client.patch(f'/api/v1/tickets/{ticket.id}',
                        json={'assignee_id': users['dev'].id})
    assert resp.status_code == 400


def test_a_ticket_can_be_assigned_to_devops(client, users, ticket):
    ops2 = make_user('ops2', 'devops')
    login(client, 'ops')
    resp = client.patch(f'/api/v1/tickets/{ticket.id}', json={'assignee_id': ops2.id})
    assert resp.status_code == 200
    assert resp.get_json()['assignee'] == 'ops2'


def test_an_unknown_project_is_rejected(client, users, ticket):
    login(client, 'ops')
    assert client.patch(f'/api/v1/tickets/{ticket.id}',
                        json={'project_id': 9999}).status_code == 400


def test_a_ticket_can_be_linked_to_a_project_at_triage(client, users, project, ticket):
    login(client, 'ops')
    resp = client.patch(f'/api/v1/tickets/{ticket.id}', json={'project_id': project.id})
    assert resp.get_json()['project'] == 'Demo'


def test_an_empty_patch_is_rejected(client, users, ticket):
    login(client, 'ops')
    assert client.patch(f'/api/v1/tickets/{ticket.id}', json={}).status_code == 400


def test_triage_leaves_a_system_note(client, users, ticket):
    login(client, 'ops')
    client.patch(f'/api/v1/tickets/{ticket.id}', json={'status': 'in_progress'})
    body = client.get(f'/api/v1/tickets/{ticket.id}').get_json()
    notes = [c for c in body['comments'] if c['is_system']]
    assert any('open' in n['body'] and 'in_progress' in n['body'] for n in notes)


def test_a_comment_can_be_added(client, users, ticket):
    login(client, 'ops')
    assert client.post(f'/api/v1/tickets/{ticket.id}/comments',
                       json={'body': 'Checking with the DBA.'}).status_code == 201
    body = client.get(f'/api/v1/tickets/{ticket.id}').get_json()
    assert body['comments'][-1]['author'] == 'ops'


def test_an_empty_comment_is_rejected(client, users, ticket):
    login(client, 'ops')
    assert client.post(f'/api/v1/tickets/{ticket.id}/comments',
                       json={'body': '   '}).status_code == 400


def test_a_missing_ticket_is_404(client, users):
    login(client, 'ops')
    assert client.get('/api/v1/tickets/9999').status_code == 404


def test_filters_narrow_the_queue(client, users, ticket):
    ops2 = make_user('ops2', 'devops')
    login(client, 'ops')
    client.patch(f'/api/v1/tickets/{ticket.id}', json={'assignee_id': ops2.id})

    assert len(client.get('/api/v1/tickets?assignee=unassigned').get_json()['tickets']) == 0
    assert len(client.get(f'/api/v1/tickets?assignee={ops2.id}').get_json()['tickets']) == 1
    assert len(client.get('/api/v1/tickets?status=resolved').get_json()['tickets']) == 0
    assert len(client.get('/api/v1/tickets?q=priya').get_json()['tickets']) == 1
    assert len(client.get('/api/v1/tickets?q=nothingmatches').get_json()['tickets']) == 0


def test_status_endpoint_is_readable_by_any_user_and_leaks_no_secret(client, users, ticket):
    login(client, 'dev')
    resp = client.get('/api/v1/tickets/status')
    assert resp.status_code == 200
    body = resp.get_json()
    # Intake is off in tests, but the queue stays visible because tickets exist.
    assert body['intake_enabled'] is False
    assert body['enabled'] is True
    assert 'secret' not in json.dumps(body).lower()


def test_the_queue_hides_itself_when_there_is_nothing_and_no_intake(client, users):
    login(client, 'dev')
    assert client.get('/api/v1/tickets/status').get_json()['enabled'] is False


def test_ticket_timestamps_carry_an_explicit_utc_offset(client, users, ticket):
    """Without the marker a browser reads naive UTC as local and ages are wrong."""
    login(client, 'ops')
    body = client.get(f'/api/v1/tickets/{ticket.id}').get_json()
    assert body['created_at'].endswith('Z') or '+' in body['created_at']


def test_the_assignee_list_offers_only_devops_and_admins(client, users, ticket):
    """A developer in the picker would let triage assign work nobody can do."""
    make_user('ops2', 'devops')
    login(client, 'ops')
    names = {a['username'] for a in
             client.get('/api/v1/tickets/assignees').get_json()['assignees']}
    assert names == {'ops', 'ops2', 'admin'}
    assert 'dev' not in names


def test_the_assignee_list_leaks_no_contact_details(client, users, ticket):
    login(client, 'ops')
    body = client.get('/api/v1/tickets/assignees').get_json()
    assert set(body['assignees'][0]) == {'id', 'username'}


def test_a_developer_cannot_read_the_assignee_list(client, users, ticket):
    login(client, 'dev')
    assert client.get('/api/v1/tickets/assignees').status_code == 403
