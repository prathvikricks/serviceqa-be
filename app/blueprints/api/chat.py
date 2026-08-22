"""Chat intake endpoints.

Authz is deliberately narrow: you must be a member of the conversation's
project to start one, and a conversation is readable only by the user who owns
it (or an admin). The transcript can contain a developer's unfiltered
description of a problem, which is not something to spread across a team by
default.
"""
import logging

from flask import jsonify, request
from flask_login import login_required, current_user

from ...extensions import db, limiter
from ...models.chat import ChatConversation
from ...models.project import Project
from ...services import chat_agent
from . import api_bp
from .helpers import _get_or_404
from .serializers import conversation_dict

logger = logging.getLogger(__name__)


def _require_enabled():
    if not chat_agent.is_enabled():
        return jsonify({'error': 'The chat assistant is not configured.'}), 503
    return None


def _owned_conversation(conversation_id):
    """Fetch a conversation the caller is allowed to read, or an error response."""
    convo = _get_or_404(ChatConversation, conversation_id)
    if convo.user_id != current_user.id and not current_user.is_admin:
        return None, (jsonify({'error': 'Not your conversation.'}), 403)
    return convo, None


@api_bp.route('/chat/status')
@login_required
def chat_status():
    """Lets the SPA hide the entry point rather than offering a dead button."""
    return jsonify({'enabled': chat_agent.is_enabled()})


@api_bp.route('/chat/conversations', methods=['POST'])
@login_required
def chat_conversation_create():
    disabled = _require_enabled()
    if disabled:
        return disabled

    data = request.get_json(silent=True) or {}
    project_id = data.get('project_id')
    _get_or_404(Project, project_id)
    if not current_user.is_member_of(project_id):
        return jsonify({'error': 'You are not a member of that project.'}), 403

    convo = ChatConversation(user_id=current_user.id, project_id=project_id)
    db.session.add(convo)
    db.session.commit()
    return jsonify(conversation_dict(convo, with_messages=True)), 201


@api_bp.route('/chat/conversations/<int:conversation_id>')
@login_required
def chat_conversation_detail(conversation_id):
    convo, denied = _owned_conversation(conversation_id)
    if denied:
        return denied
    return jsonify(conversation_dict(convo, with_messages=True))


@api_bp.route('/chat/conversations/<int:conversation_id>/messages', methods=['POST'])
@login_required
@limiter.limit('20/minute;200/hour')
def chat_message_create(conversation_id):
    disabled = _require_enabled()
    if disabled:
        return disabled

    convo, denied = _owned_conversation(conversation_id)
    if denied:
        return denied

    content = ((request.get_json(silent=True) or {}).get('content') or '').strip()
    if not content:
        return jsonify({'error': 'Say something first.'}), 400

    if convo.turn_count >= ChatConversation.MAX_TURNS:
        return jsonify({
            'error': 'This conversation has gone on long enough — start a new '
                     'one, or fill the request form directly.',
        }), 409

    try:
        result = chat_agent.respond(convo, content)
    except chat_agent.AgentUnavailable as exc:
        return jsonify({'error': str(exc)}), 503
    except chat_agent.AgentError:
        # The transcript is untouched on failure, so the developer can retry.
        return jsonify({'error': 'The assistant is having trouble right now. '
                                 'Try again in a moment.'}), 502

    return jsonify(result)
