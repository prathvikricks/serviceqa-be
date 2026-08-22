"""DevOps ticket queue.

Deliberately NOT project-scoped, unlike approvals. An email rarely says which
project it concerns, so a ticket that could only be seen by the right project's
DevOps would be a ticket nobody sees. The queue is global to devops + admins;
`project_id` is set during triage, if it turns out to matter at all.
"""
import logging
from datetime import datetime, timezone

from flask import current_app, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import or_

from ...decorators import devops_required
from ...extensions import db
from ...models.audit import AuditLog
from ...models.project import Project
from ...models.ticket import Ticket, TicketComment
from ...models.user import User
from . import api_bp
from .helpers import _get_or_404
from .serializers import ticket_dict, ticket_comment_dict

logger = logging.getLogger(__name__)

# Order the queue the way a triager reads it: unfinished work first, newest
# within each bucket.
_STATUS_ORDER = {'open': 0, 'in_progress': 1, 'resolved': 2, 'closed': 3}


def _system_note(ticket, text):
    db.session.add(TicketComment(ticket_id=ticket.id, author_id=None,
                                 body=text, is_system=True))


@api_bp.route('/tickets/status')
@login_required
def ticket_status():
    """Capability probe, so the SPA can hide a queue that cannot exist.

    `enabled` stays true once any ticket exists: a queue with history must not
    vanish because a client secret was rotated. `intake_enabled` is the separate,
    narrower question of whether mail is actually being polled.
    """
    from ...services import graph_mail

    intake = graph_mail.is_enabled()
    return jsonify({
        'enabled': intake or db.session.query(Ticket.id).first() is not None,
        'intake_enabled': intake,
        'mailbox': current_app.config.get('DEVOPS_MAILBOX'),
        'trigger_address': current_app.config.get('TICKET_TRIGGER_ADDRESS'),
        'ack_enabled': bool(current_app.config.get('TICKET_ACK_ENABLED')),
        'statuses': Ticket.STATUSES,
        'categories': Ticket.CATEGORIES,
        'urgencies': Ticket.URGENCIES,
    })


@api_bp.route('/tickets/assignees')
@login_required
@devops_required
def ticket_assignees():
    """Who a ticket may be assigned to.

    Needed because /admin/users is admin-only, but the queue is worked by
    devops — without this a triager has nothing to populate the picker with.
    Deliberately narrow: id and username only, no emails or roles.
    """
    users = User.query.filter_by(is_active=True).order_by(User.username).all()
    return jsonify({'assignees': [{'id': u.id, 'username': u.username}
                                  for u in users if u.is_devops]})


@api_bp.route('/tickets')
@login_required
@devops_required
def tickets_list():
    query = Ticket.query

    status = (request.args.get('status') or '').strip()
    if status and status != 'all':
        query = query.filter_by(status=status)

    assignee = (request.args.get('assignee') or '').strip()
    if assignee == 'me':
        query = query.filter_by(assignee_id=current_user.id)
    elif assignee == 'unassigned':
        query = query.filter(Ticket.assignee_id.is_(None))
    elif assignee.isdigit():
        query = query.filter_by(assignee_id=int(assignee))

    project_id = (request.args.get('project_id') or '').strip()
    if project_id.isdigit():
        query = query.filter_by(project_id=int(project_id))

    term = (request.args.get('q') or '').strip()
    if term:
        like = f'%{term}%'
        query = query.filter(or_(Ticket.title.ilike(like),
                                 Ticket.reference.ilike(like),
                                 Ticket.requester_email.ilike(like)))

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    pagination = query.order_by(Ticket.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False)

    rows = sorted(pagination.items,
                  key=lambda t: (_STATUS_ORDER.get(t.status, 9), -(t.id or 0)))

    counts = {s: Ticket.query.filter_by(status=s).count() for s in Ticket.STATUSES}

    return jsonify({
        'tickets': [ticket_dict(t) for t in rows],
        'statuses': Ticket.STATUSES,
        'categories': Ticket.CATEGORIES,
        'urgencies': Ticket.URGENCIES,
        'counts': counts,
        'page': pagination.page,
        'pages': pagination.pages,
        'total': pagination.total,
    })


@api_bp.route('/tickets', methods=['POST'])
@login_required
@devops_required
def ticket_create():
    """Manual ticket — for work that arrives by a tap on the shoulder."""
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'A title is required.'}), 400

    invalid = _validate_enums(data)
    if invalid:
        return invalid

    ticket = Ticket(
        title=title[:300],
        body=(data.get('body') or '').strip() or title,
        summary=(data.get('summary') or '').strip() or None,
        category=data.get('category') or None,
        urgency=data.get('urgency') or None,
        project_id=data.get('project_id') or None,
        source='manual',
        enriched_by='fallback',
        ack_state='disabled',
        requester_user_id=current_user.id,
        requester_email=current_user.email,
        requester_name=current_user.username,
    )
    db.session.add(ticket)
    db.session.flush()
    ticket.stamp_reference()
    db.session.commit()

    AuditLog.log('ticket_created', 'ticket', ticket.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'source': 'manual', 'reference': ticket.reference})
    return jsonify(ticket_dict(ticket, detail=True)), 201


def _validate_enums(data):
    """400 response for any out-of-range enum, or None."""
    checks = (('category', Ticket.CATEGORIES), ('urgency', Ticket.URGENCIES),
              ('status', Ticket.STATUSES))
    for key, allowed in checks:
        value = data.get(key)
        if value not in (None, '') and value not in allowed:
            return jsonify({'error': f'{key} must be one of: {", ".join(allowed)}.'}), 400
    return None


@api_bp.route('/tickets/<int:ticket_id>')
@login_required
@devops_required
def ticket_detail(ticket_id):
    ticket = _get_or_404(Ticket, ticket_id)
    return jsonify(ticket_dict(ticket, detail=True))


@api_bp.route('/tickets/<int:ticket_id>', methods=['PATCH'])
@login_required
@devops_required
def ticket_update(ticket_id):
    """Triage: status, assignee, project, category, urgency."""
    ticket = _get_or_404(Ticket, ticket_id)
    data = request.get_json(silent=True) or {}

    invalid = _validate_enums(data)
    if invalid:
        return invalid

    changes = []

    if 'assignee_id' in data:
        assignee_id = data['assignee_id']
        if assignee_id:
            user = db.session.get(User, assignee_id)
            # DevOps work cannot be assigned to someone with no standing to do
            # it — the queue would look handled while nobody could act.
            if user is None or not user.is_devops:
                return jsonify({'error': 'Assignee must be a devops or admin user.'}), 400
            changes.append(f'assigned to {user.username}')
        else:
            assignee_id = None
            changes.append('unassigned')
        ticket.assignee_id = assignee_id

    if 'project_id' in data:
        project_id = data['project_id'] or None
        if project_id and db.session.get(Project, project_id) is None:
            return jsonify({'error': 'Unknown project.'}), 400
        ticket.project_id = project_id
        changes.append('project updated')

    if 'status' in data and data['status'] != ticket.status:
        previous, ticket.status = ticket.status, data['status']
        ticket.resolved_at = (datetime.now(timezone.utc)
                              if ticket.status in Ticket.TERMINAL_STATUSES else None)
        changes.append(f'status {previous} → {ticket.status}')

    for field in ('category', 'urgency', 'title', 'summary'):
        if field in data:
            setattr(ticket, field, data[field] or None)
            changes.append(f'{field} updated')

    if not changes:
        return jsonify({'error': 'Nothing to update.'}), 400

    for note in changes:
        _system_note(ticket, note)
    db.session.commit()

    AuditLog.log('ticket_updated', 'ticket', ticket.id,
                 user_id=current_user.id, ip_address=request.remote_addr,
                 details={'changes': changes, 'reference': ticket.reference})
    return jsonify(ticket_dict(ticket, detail=True))


@api_bp.route('/tickets/<int:ticket_id>/comments', methods=['POST'])
@login_required
@devops_required
def ticket_comment(ticket_id):
    ticket = _get_or_404(Ticket, ticket_id)
    body = ((request.get_json(silent=True) or {}).get('body') or '').strip()
    if not body:
        return jsonify({'error': 'Say something first.'}), 400

    comment = TicketComment(ticket_id=ticket.id, author_id=current_user.id, body=body)
    db.session.add(comment)
    db.session.commit()
    return jsonify(ticket_comment_dict(comment)), 201
