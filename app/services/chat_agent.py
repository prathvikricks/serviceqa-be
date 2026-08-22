"""The chat intake agent — the single gate between this app and Gemini.

Mirrors CloudManagerFactory: one module owns the third-party integration, and
the SDK is imported lazily so a deployment that never enables the feature never
needs the dependency installed.

The contract with the model is deliberately dull. Every turn returns the same
JSON object (see RESPONSE_SCHEMA) — no tool calls, no streaming, no state held
on their side. That makes each turn one call, one parse, one validation, and
makes the whole thing testable with a stub client.
"""
import json
import logging

from flask import current_app

from ..extensions import db
from ..models.chat import ChatMessage
from ..models.request import EnvironmentRequest
from .chat_validation import validate_draft

logger = logging.getLogger(__name__)


class AgentUnavailable(Exception):
    """The feature is not configured."""


class AgentError(Exception):
    """The model call failed or returned something unusable."""


RESPONSE_SCHEMA = {
    'type': 'OBJECT',
    'required': ['reply', 'ready'],
    'properties': {
        'reply': {'type': 'STRING'},
        'ready': {'type': 'BOOLEAN'},
        'missing': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
        'request_type': {'type': 'STRING', 'enum': ['service', 'repo']},
        'draft': {
            'type': 'OBJECT',
            'properties': {
                'environment_id': {'type': 'INTEGER'},
                'service_ids': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}},
                'action_type': {'type': 'STRING', 'enum': ['start_stop', 'stop_start']},
                'schedule_type': {'type': 'STRING', 'enum': ['once', 'weekly']},
                'start_time': {'type': 'STRING'},
                'end_time': {'type': 'STRING'},
                'recurrence_days': {'type': 'STRING'},
                'start_hm': {'type': 'STRING'},
                'stop_hm': {'type': 'STRING'},
                'recur_until': {'type': 'STRING'},
                'repo_name': {'type': 'STRING'},
                'repo_description': {'type': 'STRING'},
                'repo_visibility': {'type': 'STRING', 'enum': ['private', 'public']},
                'reason': {'type': 'STRING'},
            },
        },
    },
}

_SYSTEM_INSTRUCTION = """\
You help a developer turn a vague need into one concrete request in an internal
environment-management tool. You do not perform actions; you only propose a
draft that the developer then reviews on a form.

There are exactly two request types:

- "service": schedule a start/stop window on an existing environment. Needs an
  environment, the cloud services to act on, an action type, a schedule, and a
  reason.
- "repo": ask for a new Git repository. Needs a name, visibility, and a reason.
  An approver picks GitHub or GitLab later — never choose a provider yourself.

Rules:
- Ask one focused question at a time until you can fill a complete draft.
- Only ever use the environment and service ids listed in the project context
  below. Never invent an id, and never refer to anything not listed.
- Times are naive local times in the timezone stated below. Do not convert.
- Set "ready": true and fill "draft" ONLY when every required field is known.
  Otherwise set "ready": false, leave "draft" null, and list what you still
  need in "missing".
- "reply" is shown directly to the developer. Keep it short and plain.
"""


def is_enabled():
    """True if the chat feature is configured."""
    return bool(current_app.config.get('GEMINI_API_KEY'))


def _client():
    """Build a Gemini client. Imported lazily — see the module docstring."""
    if not is_enabled():
        raise AgentUnavailable('GEMINI_API_KEY is not set.')
    try:
        from google import genai
    except ImportError as exc:   # pragma: no cover - depends on the deploy
        raise AgentUnavailable('google-genai is not installed.') from exc
    return genai.Client(api_key=current_app.config['GEMINI_API_KEY'])


def build_project_context(project):
    """Everything the model is allowed to know: this project and nothing else.

    Scoping happens here rather than in the prompt's wording. A developer cannot
    be handed a draft naming a project they are not on, because that project's
    ids never enter the conversation.
    """
    from datetime import date

    tz = current_app.config.get('SCHEDULER_TIMEZONE', 'UTC')
    lines = [
        f'Project: {project.name} (id {project.id})',
        f'Timezone for all times: {tz}',
        f"Today's date: {date.today().isoformat()}",
        f'Weekday tokens: {", ".join(EnvironmentRequest.WEEKDAYS)}',
        'Time-of-day format: HH:MM (24-hour)',
        '',
        'Environments and their cloud services:',
    ]
    for env in project.environments.all():
        lines.append(f'- environment_id {env.id}: {env.display_name} ({env.name})')
        services = env.services.all()
        if not services:
            lines.append('    (no cloud services registered)')
        for svc in services:
            lines.append(f'    service_id {svc.id}: {svc.name} [{svc.service_type}]')
    return '\n'.join(lines)


def _history_contents(conversation, user_message, correction=None):
    """The transcript in the SDK's contents format, oldest first."""
    contents = []
    for msg in conversation.messages.all():
        contents.append({
            'role': 'user' if msg.role == 'user' else 'model',
            'parts': [{'text': msg.content}],
        })
    contents.append({'role': 'user', 'parts': [{'text': user_message}]})
    if correction:
        contents.append({'role': 'user', 'parts': [{'text': correction}]})
    return contents


def _call(client, conversation, user_message, correction=None):
    model = current_app.config.get('GEMINI_MODEL', 'gemini-2.5-flash')
    config = {
        'system_instruction': _SYSTEM_INSTRUCTION + '\n\nProject context:\n'
                              + build_project_context(conversation.project),
        'response_mime_type': 'application/json',
        'response_schema': RESPONSE_SCHEMA,
    }
    try:
        response = client.models.generate_content(
            model=model,
            contents=_history_contents(conversation, user_message, correction),
            config=config,
        )
    except Exception as exc:
        logger.exception('Gemini call failed for conversation %s', conversation.id)
        raise AgentError(str(exc)) from exc

    try:
        return json.loads(response.text)
    except (TypeError, ValueError) as exc:
        raise AgentError('The model returned output that was not JSON.') from exc


def respond(conversation, user_message, client=None):
    """One turn: call the model, validate any draft, persist both messages.

    A draft that fails validation is dropped and the specific problems are fed
    back to the model once. If the retry also fails we return the reply without
    a draft rather than passing an unchecked one to the form — the model
    proposes, it never decides.
    """
    client = client or _client()

    payload = _call(client, conversation, user_message)
    request_type = payload.get('request_type')
    draft, problems = None, []

    if payload.get('ready') and payload.get('draft') is not None:
        draft, problems = validate_draft(
            conversation.project, request_type, payload['draft'])

        if draft is None:
            correction = ('That draft was rejected: ' + ' '.join(problems)
                          + ' Correct it using only the ids in the project context.')
            payload = _call(client, conversation, user_message, correction)
            request_type = payload.get('request_type')
            if payload.get('ready') and payload.get('draft') is not None:
                draft, problems = validate_draft(
                    conversation.project, request_type, payload['draft'])

    ready = draft is not None
    reply = (payload.get('reply') or '').strip() or 'Could you tell me a bit more?'

    db.session.add(ChatMessage(conversation_id=conversation.id, role='user',
                               content=user_message))
    db.session.add(ChatMessage(conversation_id=conversation.id, role='agent',
                               content=reply, draft=draft,
                               request_type=request_type if ready else None))
    db.session.commit()

    return {
        'reply': reply,
        'ready': ready,
        'missing': payload.get('missing') or problems,
        'request_type': request_type if ready else None,
        'draft': draft,
    }
