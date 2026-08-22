"""DevOps can correct a request before approving it.

A developer asks for an outcome, not a machine list. The approver knows which
environment that means and which services matter, so they fix it in place
rather than bouncing it back.
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.environment import CloudService, Environment
from app.models.request import EnvironmentRequest, RequestService

from conftest import login, make_user
from test_request_flow import _make_approver


@pytest.fixture
def second_env(app, project):
    env = Environment(project_id=project.id, name='staging', display_name='Staging')
    db.session.add(env)
    db.session.flush()
    db.session.add(CloudService(environment_id=env.id, name='Stage Web',
                                service_type='ec2', cloud_resource_id='i-stage',
                                hourly_cost=0.5, current_status='stopped'))
    db.session.commit()
    return env


@pytest.fixture
def pending(app, project, users):
    start = datetime.now() + timedelta(hours=2)
    req = EnvironmentRequest(
        requester_id=users['dev'].id, request_type='service',
        environment_id=project.environments.first().id,
        start_time=start, end_time=start + timedelta(hours=2),
        reason='demo for the client')
    db.session.add(req)
    db.session.flush()
    for svc in project.environments.first().services.all():
        db.session.add(RequestService(request_id=req.id, cloud_service_id=svc.id))
    db.session.commit()
    return req


def test_the_approver_can_move_it_to_another_environment(client, project, users,
                                                         pending, second_env):
    _make_approver(project, users['devops'])
    login(client, 'ops')

    resp = client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'environment_id': second_env.id})
    assert resp.status_code == 200
    assert resp.get_json()['environment'] == 'Staging'

    # The old environment's services must not linger on the request.
    names = {rs.cloud_service.name for rs in
             db.session.get(EnvironmentRequest, pending.id).services.all()}
    assert names == {'Stage Web'}


def test_the_approver_can_choose_which_services_start(client, project, users, pending):
    _make_approver(project, users['devops'])
    env = project.environments.first()
    only_db = [s.id for s in env.services.all() if s.name == 'DB']

    login(client, 'ops')
    resp = client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'service_ids': only_db})
    assert resp.status_code == 200

    names = {rs.cloud_service.name for rs in
             db.session.get(EnvironmentRequest, pending.id).services.all()}
    assert names == {'DB'}


def test_moving_environments_re_estimates_the_cost(client, project, users,
                                                   pending, second_env):
    _make_approver(project, users['devops'])
    login(client, 'ops')

    resp = client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'environment_id': second_env.id})
    # Staging is 0.50/hr over two hours; the old env was 0.30/hr.
    assert resp.get_json()['estimated_cost'] == pytest.approx(1.0)


def test_an_environment_from_another_project_is_refused(client, project, users, pending):
    from app.models.project import Project

    other = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                    created_by=users['admin'].id)
    other.set_provider_config({'region': 'us-east-1'})
    db.session.add(other)
    db.session.flush()
    foreign = Environment(project_id=other.id, name='dev', display_name='Dev')
    db.session.add(foreign)
    db.session.commit()

    _make_approver(project, users['devops'])
    login(client, 'ops')
    assert client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'environment_id': foreign.id}).status_code == 400


def test_a_service_outside_the_environment_is_refused(client, project, users,
                                                      pending, second_env):
    _make_approver(project, users['devops'])
    stray = second_env.services.first().id

    login(client, 'ops')
    assert client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'service_ids': [stray]}).status_code == 400


def test_a_developer_cannot_adjust(client, project, users, pending):
    login(client, 'dev')
    assert client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'service_ids': []}).status_code == 403


def test_another_projects_devops_cannot_adjust(client, project, users, pending):
    outsider = make_user('ops2', 'devops')
    login(client, 'ops2')
    assert client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'service_ids': []}).status_code == 403


def test_an_already_approved_request_cannot_be_adjusted(client, project, users, pending):
    _make_approver(project, users['devops'])
    pending.status = 'approved'
    db.session.commit()

    login(client, 'ops')
    resp = client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={'service_ids': []})
    assert resp.status_code == 400
    assert 'pending' in resp.get_json()['error']


def test_an_empty_adjustment_is_rejected(client, project, users, pending):
    _make_approver(project, users['devops'])
    login(client, 'ops')
    assert client.patch(f'/api/v1/approvals/{pending.id}/adjust',
                        json={}).status_code == 400
