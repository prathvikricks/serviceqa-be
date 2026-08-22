"""The agent service: prompt scope, parsing, and what happens to a bad draft.

Tests inject a stub client — no network, and the suite passes with no API key.
"""
import json
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.chat import ChatConversation
from app.services import chat_agent


class StubResponse:
    def __init__(self, payload):
        self.text = json.dumps(payload)


class StubClient:
    """Records what it was asked and replays a queued list of payloads."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        self.calls.append({'model': model, 'contents': contents, 'config': config})
        return StubResponse(self.payloads.pop(0))


def _convo(project, users):
    convo = ChatConversation(user_id=users['dev'].id, project_id=project.id)
    db.session.add(convo)
    db.session.commit()
    return convo


def test_the_prompt_only_describes_the_conversations_project(app, project, users):
    context = chat_agent.build_project_context(project)

    assert project.name in context
    assert 'a-project-they-cannot-see' not in context
    for svc in project.environments.first().services.all():
        assert svc.name in context


def test_a_follow_up_turn_returns_no_draft(app, project, users):
    convo = _convo(project, users)
    client = StubClient({
        'reply': 'Which environment do you mean?',
        'ready': False, 'missing': ['environment_id'],
        'request_type': 'service', 'draft': None,
    })

    out = chat_agent.respond(convo, 'I need something up next week', client=client)

    assert out['ready'] is False
    assert out['draft'] is None
    assert 'Which environment' in out['reply']


def test_a_ready_turn_returns_a_validated_draft(app, project, users):
    convo = _convo(project, users)
    env = project.environments.first()
    start = datetime.now() + timedelta(days=1)
    client = StubClient({
        'reply': "Here's the request I'd raise.",
        'ready': True, 'missing': [], 'request_type': 'service',
        'draft': {
            'environment_id': env.id,
            'service_ids': [s.id for s in env.services.all()],
            'action_type': 'start_stop', 'schedule_type': 'once',
            'start_time': start.replace(microsecond=0).isoformat(),
            'end_time': (start + timedelta(hours=8)).replace(microsecond=0).isoformat(),
            'reason': 'client demo',
        },
    })

    out = chat_agent.respond(convo, 'UAT up all day tomorrow for the demo', client=client)

    assert out['ready'] is True
    assert out['draft']['environment_id'] == env.id


def test_an_invalid_draft_is_dropped_and_retried_once(app, project, users):
    """A draft naming a foreign service must not reach the caller."""
    convo = _convo(project, users)
    env = project.environments.first()
    start = datetime.now() + timedelta(days=1)
    bad = {
        'reply': 'Ready.', 'ready': True, 'missing': [], 'request_type': 'service',
        'draft': {
            'environment_id': env.id, 'service_ids': [9999],
            'action_type': 'start_stop', 'schedule_type': 'once',
            'start_time': start.isoformat(),
            'end_time': (start + timedelta(hours=1)).isoformat(),
            'reason': 'x',
        },
    }
    client = StubClient(bad, bad)     # fails, retried, fails again

    out = chat_agent.respond(convo, 'anything', client=client)

    assert out['ready'] is False
    assert out['draft'] is None
    assert len(client.calls) == 2, 'a rejected draft should be fed back once'


def test_unparseable_output_raises_agent_error(app, project, users):
    class Broken(StubClient):
        def generate_content(self, model=None, contents=None, config=None):
            self.calls.append(1)
            return type('R', (), {'text': 'not json at all'})()

    with pytest.raises(chat_agent.AgentError):
        chat_agent.respond(_convo(project, users), 'hi', client=Broken())


def test_the_turn_is_persisted(app, project, users):
    convo = _convo(project, users)
    client = StubClient({'reply': 'ok', 'ready': False, 'missing': [],
                         'request_type': None, 'draft': None})

    chat_agent.respond(convo, 'hello there', client=client)

    roles = [m.role for m in convo.messages.all()]
    assert roles == ['user', 'agent']
    assert convo.messages.filter_by(role='user').first().content == 'hello there'


def test_disabled_without_an_api_key(app):
    app.config['GEMINI_API_KEY'] = None
    assert chat_agent.is_enabled() is False
