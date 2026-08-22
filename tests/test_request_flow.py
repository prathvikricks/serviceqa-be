"""The core loop: a developer requests a window, devops approves, the scheduler
starts and stops the environment's services."""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.request import EnvironmentRequest
from app.models.environment import CloudService
from app.models.user import ProjectMember
from app.services.scheduler_service import _start_environment, _stop_environment

from conftest import login


def _env_id(project):
    return project.environments.first().id


def _make_approver(project, user):
    """Approval is project-scoped: a devops must be on the project to act on it."""
    member = project.members.filter_by(user_id=user.id).first()
    if member is None:
        member = ProjectMember(project_id=project.id, user_id=user.id,
                               added_by=user.id)
        db.session.add(member)
    member.project_role = 'devops'
    db.session.commit()
    return member


def _reload(model, pk):
    """Re-read a row the scheduler mutated.

    The scheduler pushes its own app context, so its commits land in a
    different session — without expiring, the test session would hand back its
    cached (pre-job) copy.
    """
    db.session.expire_all()
    return db.session.get(model, pk)


def test_developer_creates_request_pending_approval(client, project, users):
    login(client, 'dev')
    start = datetime.now() + timedelta(hours=2)
    resp = client.post('/api/v1/requests', json={
        'environment_id': _env_id(project),
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=4)).isoformat(),
        'reason': 'Regression testing',
    })
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body['status'] == 'pending'
    # 0.10 + 0.20 per hour across 4 hours.
    assert body['estimated_cost'] == 1.2
    # Every active service in the environment is attached to the request.
    assert len(body['services']) == 2


def test_developer_cannot_approve(client, project, users):
    login(client, 'dev')
    start = datetime.now() + timedelta(hours=2)
    rid = client.post('/api/v1/requests', json={
        'environment_id': _env_id(project),
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=1)).isoformat(),
        'reason': 'nope',
    }).get_json()['id']

    assert client.post(f'/api/v1/approvals/{rid}/approve', json={}).status_code == 403


def test_approve_then_start_and_stop_flips_service_status(client, app, project, users):
    login(client, 'dev')
    start = datetime.now() + timedelta(hours=2)
    rid = client.post('/api/v1/requests', json={
        'environment_id': _env_id(project),
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=3)).isoformat(),
        'reason': 'Load test',
    }).get_json()['id']

    client.post('/api/v1/auth/logout')
    _make_approver(project, users['devops'])
    login(client, 'ops')
    resp = client.post(f'/api/v1/approvals/{rid}/approve', json={'comment': 'ok'})
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'approved'

    # Drive the scheduler's jobs directly — the window is 2h out, so approving
    # only arms them.
    _start_environment(app, rid)
    assert _reload(EnvironmentRequest, rid).status == 'active'
    assert {s.current_status for s in CloudService.query.all()} == {'running'}

    _stop_environment(app, rid)
    assert _reload(EnvironmentRequest, rid).status == 'completed'
    assert {s.current_status for s in CloudService.query.all()} == {'stopped'}


def test_declined_request_never_schedules(client, app, project, users):
    login(client, 'dev')
    start = datetime.now() + timedelta(hours=2)
    rid = client.post('/api/v1/requests', json={
        'environment_id': _env_id(project),
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=1)).isoformat(),
        'reason': 'too expensive',
    }).get_json()['id']

    client.post('/api/v1/auth/logout')
    _make_approver(project, users['devops'])
    login(client, 'ops')
    client.post(f'/api/v1/approvals/{rid}/decline', json={'comment': 'use dev'})

    req = _reload(EnvironmentRequest, rid)
    assert req.status == 'declined'
    assert req.scheduled_jobs.count() == 0

    # A declined request must not be startable even if a job somehow fires.
    _start_environment(app, rid)
    assert _reload(EnvironmentRequest, rid).status == 'declined'


def test_weekly_request_computes_next_occurrence(client, project, users):
    login(client, 'dev')
    resp = client.post('/api/v1/requests', json={
        'environment_id': _env_id(project),
        'schedule_type': 'weekly',
        'recurrence_days': ['mon', 'wed', 'fri'],
        'start_hm': '09:00',
        'stop_hm': '17:00',
        'reason': 'Business hours only',
    })
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body['schedule_type'] == 'weekly'
    assert body['recurrence_days'] == ['mon', 'wed', 'fri']
    assert 'Mon, Wed, Fri' in body['recurrence_label']
    # start/end are populated with the next upcoming occurrence.
    assert body['start_time'].endswith('09:00:00')
    assert body['end_time'].endswith('17:00:00')
    assert body['duration_hours'] == 8.0


def test_weekly_rejects_backwards_window(client, project, users):
    login(client, 'dev')
    resp = client.post('/api/v1/requests', json={
        'environment_id': _env_id(project),
        'schedule_type': 'weekly',
        'recurrence_days': ['mon'],
        'start_hm': '17:00',
        'stop_hm': '09:00',
        'reason': 'backwards',
    })
    assert resp.status_code == 400
    assert 'after' in resp.get_json()['error']


def test_developer_only_sees_own_requests(client, project, users):
    login(client, 'dev')
    start = datetime.now() + timedelta(hours=2)
    client.post('/api/v1/requests', json={
        'environment_id': _env_id(project),
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=1)).isoformat(),
        'reason': 'mine',
    })
    client.post('/api/v1/auth/logout')

    # A second developer sees an empty list, not the first one's request.
    from conftest import make_user
    make_user('dev2', 'developer')
    login(client, 'dev2')
    assert client.get('/api/v1/requests').get_json()['requests'] == []

    # devops sees it.
    client.post('/api/v1/auth/logout')
    login(client, 'ops')
    assert len(client.get('/api/v1/requests').get_json()['requests']) == 1
