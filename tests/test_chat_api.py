"""Chat conversations: ownership, turn limits, and the disabled-by-default gate."""
from app.extensions import db
from app.models.chat import ChatConversation, ChatMessage

from conftest import login, make_user


def test_a_conversation_holds_ordered_messages(project, users):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.flush()
    db.session.add(ChatMessage(conversation_id=convo.id, role='user',
                               content='I need UAT up next week'))
    db.session.add(ChatMessage(conversation_id=convo.id, role='agent',
                               content='Which services?'))
    db.session.commit()

    assert convo.messages.count() == 2
    assert convo.turn_count == 1          # one user turn


def test_deleting_a_conversation_deletes_its_messages(project, users):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.flush()
    db.session.add(ChatMessage(conversation_id=convo.id, role='user', content='hi'))
    db.session.commit()

    db.session.delete(convo)
    db.session.commit()
    assert ChatMessage.query.count() == 0


# --- endpoints ---------------------------------------------------------------

import pytest  # noqa: E402

from app.services import chat_agent  # noqa: E402


@pytest.fixture
def enabled(app):
    app.config['GEMINI_API_KEY'] = 'test-key'
    yield
    app.config['GEMINI_API_KEY'] = None


def test_status_reports_disabled_without_a_key(client, users):
    login(client, 'dev')
    assert client.get('/api/v1/chat/status').get_json() == {'enabled': False}


def test_endpoints_return_503_when_disabled(client, project, users):
    login(client, 'dev')
    resp = client.post('/api/v1/chat/conversations', json={'project_id': project.id})
    assert resp.status_code == 503


def test_a_member_can_open_a_conversation(client, project, users, enabled):
    login(client, 'dev')
    resp = client.post('/api/v1/chat/conversations', json={'project_id': project.id})
    assert resp.status_code == 201
    assert resp.get_json()['project_id'] == project.id


def test_a_non_member_cannot_open_a_conversation(client, project, users, enabled):
    make_user('outsider', 'developer')
    login(client, 'outsider')
    resp = client.post('/api/v1/chat/conversations', json={'project_id': project.id})
    assert resp.status_code == 403


def test_another_user_cannot_read_your_conversation(client, project, users, enabled):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.commit()

    make_user('nosy', 'developer')
    login(client, 'nosy')
    assert client.get(f'/api/v1/chat/conversations/{convo.id}').status_code == 403


def test_sending_a_message_returns_the_agents_reply(client, project, users, enabled,
                                                    monkeypatch):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.commit()

    def fake_respond(conversation, content, client=None):
        return {'reply': 'Which environment?', 'ready': False,
                'missing': ['environment_id'], 'request_type': None, 'draft': None}

    monkeypatch.setattr(chat_agent, 'respond', fake_respond)

    login(client, 'dev')
    resp = client.post(f'/api/v1/chat/conversations/{convo.id}/messages',
                       json={'content': 'I need something'})
    assert resp.status_code == 200
    assert resp.get_json()['reply'] == 'Which environment?'


def test_a_model_failure_is_a_502(client, project, users, enabled, monkeypatch):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.commit()

    def boom(conversation, content, client=None):
        raise chat_agent.AgentError('upstream exploded')

    monkeypatch.setattr(chat_agent, 'respond', boom)

    login(client, 'dev')
    resp = client.post(f'/api/v1/chat/conversations/{convo.id}/messages',
                       json={'content': 'hi'})
    assert resp.status_code == 502


def test_the_turn_cap_is_enforced(client, project, users, enabled):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.flush()
    for i in range(ChatConversation.MAX_TURNS):
        db.session.add(ChatMessage(conversation_id=convo.id, role='user',
                                   content=f'turn {i}'))
    db.session.commit()

    login(client, 'dev')
    resp = client.post(f'/api/v1/chat/conversations/{convo.id}/messages',
                       json={'content': 'one more'})
    assert resp.status_code == 409


def test_an_empty_message_is_rejected(client, project, users, enabled):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.commit()

    login(client, 'dev')
    resp = client.post(f'/api/v1/chat/conversations/{convo.id}/messages',
                       json={'content': '   '})
    assert resp.status_code == 400


# --- provenance: which conversation produced a request -----------------------

from datetime import datetime, timedelta  # noqa: E402

from app.models.request import EnvironmentRequest  # noqa: E402


def test_a_request_records_the_conversation_that_produced_it(client, project, users, enabled):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.commit()

    start = datetime.now() + timedelta(hours=2)
    login(client, 'dev')
    resp = client.post('/api/v1/requests', json={
        'environment_id': project.environments.first().id,
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=2)).isoformat(),
        'reason': 'from chat',
        'conversation_id': convo.id,
    })
    assert resp.status_code == 201
    created = db.session.get(EnvironmentRequest, resp.get_json()['id'])
    assert created.conversation_id == convo.id


def test_someone_elses_conversation_id_is_ignored(client, project, users, enabled):
    convo = ChatConversation(user_id=users['admin'].id, project_id=project.id)
    db.session.add(convo)
    db.session.commit()

    start = datetime.now() + timedelta(hours=2)
    login(client, 'dev')
    resp = client.post('/api/v1/requests', json={
        'environment_id': project.environments.first().id,
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=2)).isoformat(),
        'reason': 'not mine',
        'conversation_id': convo.id,
    })
    assert resp.status_code == 201
    created = db.session.get(EnvironmentRequest, resp.get_json()['id'])
    assert created.conversation_id is None
