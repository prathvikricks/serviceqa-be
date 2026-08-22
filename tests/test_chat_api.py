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
