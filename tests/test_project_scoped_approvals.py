"""Approval rights are scoped to the project a request belongs to.

Membership says *who is on* a project; `project_role` says *what they are* on
it. Only a project-devops (or an admin) may approve that project's requests.
"""
from app.extensions import db
from app.models.user import ProjectMember

from conftest import login, make_user


def _member(project, user, project_role='developer'):
    """Add `user` to `project` with the given project role, or update it."""
    existing = project.members.filter_by(user_id=user.id).first()
    if existing:
        existing.project_role = project_role
        db.session.commit()
        return existing
    m = ProjectMember(project_id=project.id, user_id=user.id,
                      added_by=user.id, project_role=project_role)
    db.session.add(m)
    db.session.commit()
    return m


def test_membership_defaults_to_developer(project, users):
    member = project.members.filter_by(user_id=users['dev'].id).first()
    assert member.project_role == 'developer'


def test_project_devops_predicate(project, users):
    ops = make_user('ops2', 'devops')
    # A global devops with no project role does not gain approval rights.
    assert ops.is_project_devops(project.id) is False

    _member(project, ops, 'devops')
    assert ops.is_project_devops(project.id) is True


def test_developer_membership_grants_no_approval_rights(project, users):
    assert users['dev'].is_project_devops(project.id) is False


def test_admin_is_always_a_project_approver(project, users):
    assert users['admin'].is_project_devops(project.id) is True


# --- approvals inbox scoping ------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402

from app.models.project import Project  # noqa: E402
from app.models.environment import Environment  # noqa: E402
from app.models.request import EnvironmentRequest  # noqa: E402


def _second_project(users):
    """A second project with its own environment, so scoping is observable."""
    p = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                created_by=users['admin'].id)
    p.set_provider_config({'region': 'us-east-1'})
    db.session.add(p)
    db.session.flush()
    env = Environment(project_id=p.id, name='dev', display_name='Dev')
    db.session.add(env)
    db.session.commit()
    return p


def _service_request(env_id, requester_id):
    start = datetime.now() + timedelta(hours=2)
    req = EnvironmentRequest(
        requester_id=requester_id, request_type='service', environment_id=env_id,
        start_time=start, end_time=start + timedelta(hours=1),
        reason='scoping fixture')
    db.session.add(req)
    db.session.commit()
    return req


def _repo_request(project_id, requester_id):
    req = EnvironmentRequest(
        requester_id=requester_id, request_type='repo', project_id=project_id,
        action_type='create_repo', repo_name='billing-svc',
        repo_visibility='private', reason='scoping fixture')
    db.session.add(req)
    db.session.commit()
    return req


def test_approvals_list_shows_only_your_projects(client, project, users):
    other = _second_project(users)
    mine = _service_request(project.environments.first().id, users['dev'].id)
    theirs = _service_request(other.environments.first().id, users['dev'].id)

    ops = make_user('ops2', 'devops')
    _member(project, ops, 'devops')

    login(client, 'ops2')
    ids = {r['id'] for r in client.get('/api/v1/approvals').get_json()['requests']}
    assert mine.id in ids
    assert theirs.id not in ids


def test_approvals_list_includes_repo_requests_of_your_projects(client, project, users):
    other = _second_project(users)
    mine = _repo_request(project.id, users['dev'].id)
    theirs = _repo_request(other.id, users['dev'].id)

    ops = make_user('ops2', 'devops')
    _member(project, ops, 'devops')

    login(client, 'ops2')
    ids = {r['id'] for r in client.get('/api/v1/approvals').get_json()['requests']}
    assert mine.id in ids
    assert theirs.id not in ids


def test_admin_sees_every_project(client, project, users):
    other = _second_project(users)
    mine = _service_request(project.environments.first().id, users['dev'].id)
    theirs = _service_request(other.environments.first().id, users['dev'].id)

    login(client, 'admin')
    ids = {r['id'] for r in client.get('/api/v1/approvals').get_json()['requests']}
    assert {mine.id, theirs.id} <= ids


def test_devops_with_no_project_role_sees_an_empty_inbox(client, project, users):
    _service_request(project.environments.first().id, users['dev'].id)
    login(client, 'ops')          # global devops, no project_role anywhere
    assert client.get('/api/v1/approvals').get_json()['requests'] == []


def test_plain_developer_is_denied_the_approvals_list(client, project, users):
    login(client, 'dev')
    assert client.get('/api/v1/approvals').status_code == 403


# --- per-request approval checks --------------------------------------------

def test_cannot_approve_another_projects_request_by_id(client, project, users):
    other = _second_project(users)
    theirs = _service_request(other.environments.first().id, users['dev'].id)

    ops = make_user('ops2', 'devops')
    _member(project, ops, 'devops')

    login(client, 'ops2')
    assert client.post(f'/api/v1/approvals/{theirs.id}/approve',
                       json={'comment': 'sneaking in'}).status_code == 403
    assert client.post(f'/api/v1/approvals/{theirs.id}/decline',
                       json={'comment': 'sneaking in'}).status_code == 403


def test_cannot_approve_another_projects_repo_request_by_id(client, project, users):
    other = _second_project(users)
    theirs = _repo_request(other.id, users['dev'].id)

    ops = make_user('ops2', 'devops')
    _member(project, ops, 'devops')

    login(client, 'ops2')
    assert client.post(f'/api/v1/approvals/{theirs.id}/approve',
                       json={'provider': 'github'}).status_code == 403


def test_project_devops_can_approve_their_own_projects_request(client, project, users):
    mine = _service_request(project.environments.first().id, users['dev'].id)

    ops = make_user('ops2', 'devops')
    _member(project, ops, 'devops')

    login(client, 'ops2')
    resp = client.post(f'/api/v1/approvals/{mine.id}/approve', json={'comment': 'ok'})
    assert resp.status_code == 200
    assert db.session.get(EnvironmentRequest, mine.id).status != 'pending'


# --- admin management of project roles ---------------------------------------

def test_admin_can_add_a_member_as_project_devops(client, project, users):
    make_user('ops2', 'devops')
    login(client, 'admin')

    resp = client.post(f'/api/v1/admin/projects/{project.id}/members',
                       json={'username': 'ops2', 'project_role': 'devops'})
    assert resp.status_code == 201
    assert resp.get_json()['project_role'] == 'devops'


def test_admin_can_change_a_members_project_role(client, project, users):
    member = project.members.filter_by(user_id=users['dev'].id).first()
    login(client, 'admin')

    resp = client.put(f'/api/v1/admin/projects/{project.id}/members/{member.id}',
                      json={'project_role': 'devops'})
    assert resp.status_code == 200
    assert resp.get_json()['project_role'] == 'devops'


def test_an_unknown_project_role_is_rejected(client, project, users):
    member = project.members.filter_by(user_id=users['dev'].id).first()
    login(client, 'admin')

    resp = client.put(f'/api/v1/admin/projects/{project.id}/members/{member.id}',
                      json={'project_role': 'superuser'})
    assert resp.status_code == 400
