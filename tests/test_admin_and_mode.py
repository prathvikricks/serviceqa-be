"""Admin CRUD, credential handling, and the mock/real gate."""
import pytest

from app.extensions import db
from app.models.project import Project
from app.services.cloud_manager import CloudManagerFactory
from app.services.mock_manager import MockManager

from conftest import login


def test_non_admin_cannot_reach_admin_api(client, project, users):
    login(client, 'dev')
    assert client.get('/api/v1/admin/projects').status_code == 403
    client.post('/api/v1/auth/logout')
    # devops is trusted to approve, but not to define projects.
    login(client, 'ops')
    assert client.get('/api/v1/admin/projects').status_code == 403


def test_create_project_encrypts_secret_and_never_returns_it(client, app, users):
    login(client, 'admin')
    resp = client.post('/api/v1/admin/projects', json={
        'name': 'Payments',
        'cloud_provider': 'aws',
        'mode': 'real',
        'aws_region': 'eu-west-1',
        'aws_access_key_id': 'AKIAEXAMPLE',
        'aws_secret_access_key': 'super-secret-value',
    })
    assert resp.status_code == 201, resp.get_json()
    pid = resp.get_json()['id']

    project = db.session.get(Project, pid)
    # The raw column holds ciphertext, never the plaintext secret.
    raw = project.provider_config['secret_access_key']
    assert isinstance(raw, dict) and '_enc' in raw
    assert 'super-secret-value' not in str(project.provider_config)
    # …and it round-trips.
    assert project.get_provider_config()['secret_access_key'] == 'super-secret-value'

    # The API surfaces only whether it's set.
    detail = client.get(f'/api/v1/admin/projects/{pid}').get_json()
    assert detail['provider_config']['secret_access_key_set'] is True
    assert 'super-secret-value' not in resp.get_data(as_text=True)
    assert 'super-secret-value' not in str(detail)


def test_blank_secret_on_edit_keeps_the_stored_one(client, app, users):
    login(client, 'admin')
    pid = client.post('/api/v1/admin/projects', json={
        'name': 'Payments', 'cloud_provider': 'aws', 'mode': 'real',
        'aws_access_key_id': 'AKIA1', 'aws_secret_access_key': 'keep-me',
    }).get_json()['id']

    # The edit form never receives the secret back, so it submits it blank.
    resp = client.put(f'/api/v1/admin/projects/{pid}', json={
        'name': 'Payments EU', 'cloud_provider': 'aws', 'mode': 'real',
        'aws_access_key_id': 'AKIA1', 'aws_secret_access_key': '',
    })
    assert resp.status_code == 200
    assert db.session.get(Project, pid).get_provider_config()['secret_access_key'] == 'keep-me'


def test_mock_project_never_builds_a_real_manager(app, project):
    """The gate that keeps a demo from touching a live cloud account."""
    assert project.mode == 'mock'
    assert isinstance(CloudManagerFactory.get_manager(project), MockManager)


def test_switching_to_real_mode_swaps_the_manager(app, project):
    from app.services.aws_manager import AWSManager

    assert isinstance(CloudManagerFactory.get_manager(project), MockManager)
    project.mode = 'real'
    project.set_provider_config({'region': 'us-east-1',
                                 'access_key_id': 'AKIA', 'secret_access_key': 's'})
    db.session.commit()
    CloudManagerFactory.clear_cache()
    assert isinstance(CloudManagerFactory.get_manager(project), AWSManager)


def test_duplicate_project_name_rejected(client, project, users):
    login(client, 'admin')
    resp = client.post('/api/v1/admin/projects',
                       json={'name': 'Demo', 'cloud_provider': 'aws', 'mode': 'mock'})
    assert resp.status_code == 400
    assert 'already exists' in resp.get_json()['error']


def test_service_type_must_match_provider(client, project, users):
    login(client, 'admin')
    eid = project.environments.first().id
    # 'vm' is Azure-only; this project is AWS.
    resp = client.post(f'/api/v1/admin/environments/{eid}/services', json={
        'name': 'Bad', 'service_type': 'vm', 'cloud_resource_id': 'x', 'hourly_cost': 1,
    })
    assert resp.status_code == 400
    assert 'Invalid service type' in resp.get_json()['error']


def test_cannot_delete_project_with_open_requests(client, project, users):
    from datetime import datetime, timedelta
    login(client, 'dev')
    start = datetime.now() + timedelta(hours=2)
    client.post('/api/v1/requests', json={
        'environment_id': project.environments.first().id,
        'start_time': start.isoformat(),
        'end_time': (start + timedelta(hours=1)).isoformat(),
        'reason': 'in flight',
    })
    client.post('/api/v1/auth/logout')

    login(client, 'admin')
    resp = client.delete(f'/api/v1/admin/projects/{project.id}')
    assert resp.status_code == 400
    assert 'open request' in resp.get_json()['error']


def test_admin_cannot_demote_the_last_admin(client, users):
    login(client, 'admin')
    resp = client.put(f"/api/v1/admin/users/{users['admin'].id}",
                      json={'role': 'developer'})
    assert resp.status_code == 400
    assert 'only admin' in resp.get_json()['error']


def test_discovery_in_mock_mode_returns_catalogue(client, project, users):
    login(client, 'ops')
    resp = client.get(f'/api/v1/projects/{project.id}/discover/ec2')
    assert resp.status_code == 200
    resources = resp.get_json()['resources']
    assert resources and all({'id', 'name', 'status'} <= set(r) for r in resources)
