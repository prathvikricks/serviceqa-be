"""Project access is scoped by membership.

A developer reaches only the projects they've been added to. DevOps and admins
keep global reach on purpose — a request in a project no DevOps belongs to would
otherwise be unapprovable forever.
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.user import ProjectMember

from conftest import login, make_user


def _window():
    start = datetime.now() + timedelta(hours=2)
    return start.isoformat(), (start + timedelta(hours=1)).isoformat()


def _project_scoped_gets(project):
    """Every read route that must respect project membership."""
    eid = project.environments.first().id
    return [
        f'/api/v1/projects/{project.id}/environments',
        f'/api/v1/projects/{project.id}/secrets',
        f'/api/v1/environments/{eid}/status',
    ]


@pytest.fixture
def outsider(app, project):
    """An active developer who is deliberately NOT a member of `project`."""
    return make_user('outsider', 'developer')


def test_non_member_developer_is_denied_every_project_route(client, project, outsider):
    login(client, 'outsider')

    for path in _project_scoped_gets(project):
        assert client.get(path).status_code == 403, f'{path} leaked to a non-member'

    start, end = _window()
    resp = client.post('/api/v1/requests', json={
        'environment_id': project.environments.first().id,
        'start_time': start, 'end_time': end, 'reason': 'should not be allowed',
    })
    assert resp.status_code == 403


def test_non_member_developer_sees_no_projects(client, project, outsider):
    login(client, 'outsider')
    assert client.get('/api/v1/projects').get_json()['projects'] == []
    # …and the dashboard counts nothing it can't see.
    stats = client.get('/api/v1/dashboard').get_json()['stats']
    assert stats['total_projects'] == 0
    assert stats['total_environments'] == 0


def test_adding_the_membership_grants_access(client, project, outsider, users):
    login(client, 'outsider')
    assert client.get(f'/api/v1/projects/{project.id}/environments').status_code == 403
    client.post('/api/v1/auth/logout')

    login(client, 'admin')
    resp = client.post(f'/api/v1/admin/projects/{project.id}/members',
                       json={'username': 'outsider'})
    assert resp.status_code == 201, resp.get_json()
    client.post('/api/v1/auth/logout')

    login(client, 'outsider')
    for path in _project_scoped_gets(project):
        assert client.get(path).status_code == 200, f'{path} still denied to a member'
    assert len(client.get('/api/v1/projects').get_json()['projects']) == 1


def test_removing_the_membership_revokes_access(client, project, users):
    # The 'dev' user is a member via the fixture.
    login(client, 'dev')
    assert client.get(f'/api/v1/projects/{project.id}/environments').status_code == 200
    client.post('/api/v1/auth/logout')

    member = ProjectMember.query.filter_by(
        project_id=project.id, user_id=users['dev'].id).first()
    login(client, 'admin')
    assert client.delete(
        f'/api/v1/admin/projects/{project.id}/members/{member.id}').status_code == 200
    client.post('/api/v1/auth/logout')

    login(client, 'dev')
    for path in _project_scoped_gets(project):
        assert client.get(path).status_code == 403, f'{path} still reachable after removal'


def test_devops_reaches_a_project_they_are_not_a_member_of(client, project, users):
    """The deliberate carve-out: approvals would deadlock without it."""
    assert ProjectMember.query.filter_by(
        project_id=project.id, user_id=users['devops'].id).first() is None

    login(client, 'ops')
    for path in _project_scoped_gets(project):
        assert client.get(path).status_code == 200
    assert len(client.get('/api/v1/projects').get_json()['projects']) == 1


def test_member_add_accepts_a_username(client, project, outsider, users):
    """The SPA sends a username, not an id — regression test for a member-add
    path that always failed with 'Unknown user.'"""
    login(client, 'admin')
    resp = client.post(f'/api/v1/admin/projects/{project.id}/members',
                       json={'username': 'outsider'})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body['username'] == 'outsider'
    # Secret access is never granted implicitly by adding someone.
    assert body['can_view_secrets'] is False


def test_member_add_rejects_unknown_and_duplicate(client, project, users):
    login(client, 'admin')
    assert client.post(f'/api/v1/admin/projects/{project.id}/members',
                       json={'username': 'ghost'}).status_code == 400
    # 'dev' is already a member via the fixture.
    resp = client.post(f'/api/v1/admin/projects/{project.id}/members',
                       json={'username': 'dev'})
    assert resp.status_code == 400
    assert 'already a member' in resp.get_json()['error']


def test_developer_cannot_read_another_members_request(client, project, users):
    """Membership lets you raise requests; it doesn't expose everyone else's."""
    login(client, 'dev')
    start, end = _window()
    rid = client.post('/api/v1/requests', json={
        'environment_id': project.environments.first().id,
        'start_time': start, 'end_time': end, 'reason': 'mine',
    }).get_json()['id']
    client.post('/api/v1/auth/logout')

    # A second member of the same project still doesn't see it in their list.
    second = make_user('dev2', 'developer')
    db.session.add(ProjectMember(project_id=project.id, user_id=second.id,
                                 added_by=users['admin'].id))
    db.session.commit()

    login(client, 'dev2')
    assert client.get('/api/v1/requests').get_json()['requests'] == []
    client.post('/api/v1/auth/logout')

    # A non-member is denied the detail view outright.
    make_user('nobody', 'developer')
    login(client, 'nobody')
    assert client.get(f'/api/v1/requests/{rid}').status_code == 403
