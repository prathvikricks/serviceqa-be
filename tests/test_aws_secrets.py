"""Central AWS Secrets Manager: mapping AWS secrets to projects and revealing
them live. AWS itself is stubbed — these tests exercise the mapping, permission,
and audit logic, not boto3.
"""
import pytest

from app.extensions import db
from app.models.audit import AuditLog
from app.models.project import Project
from app.models.project_aws_secret import ProjectAwsSecret
from app.models.setting import Setting
from app.models.user import ProjectMember
from app.services import aws_manager, secrets_manager

from conftest import login

ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:demo/DB_PASSWORD-a1b2'
VALUE = 'live-db-password'


@pytest.fixture
def aws_configured(app):
    """Seed the three global AWS Settings rows and clear the manager cache."""
    for key, val in [('AWS_ACCESS_KEY_ID', 'AKIA_TEST'),
                     ('AWS_SECRET_ACCESS_KEY', 'shhh'),
                     ('AWS_REGION', 'us-east-1')]:
        row = Setting(key=key)
        row.set_value(val)
        db.session.add(row)
    db.session.commit()
    secrets_manager.reset_cache()
    yield
    secrets_manager.reset_cache()


def _stub_aws(monkeypatch, entries=None, value=VALUE):
    if entries is None:
        entries = [{'name': 'demo/DB_PASSWORD', 'arn': ARN, 'description': 'db'}]
    monkeypatch.setattr(aws_manager.AWSManager, 'list_all_secrets',
                        lambda self, region=None: entries)
    monkeypatch.setattr(aws_manager.AWSManager, 'get_secret_string',
                        lambda self, secret_id, region=None: value)


def _grant(project, user, can_view):
    m = ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
    if m is None:
        m = ProjectMember(project_id=project.id, user_id=user.id, added_by=user.id)
        db.session.add(m)
    m.can_view_secrets = can_view
    db.session.commit()
    return m


def _associate(client, project, arn=ARN, name='demo/DB_PASSWORD', environment_id=None):
    return client.post(f'/api/v1/admin/projects/{project.id}/aws-secrets',
                       json={'aws_arn': arn, 'aws_name': name,
                             'environment_id': environment_id})


# --- configuration guard ---------------------------------------------------

def test_central_list_requires_configuration(client, users):
    login(client, 'admin')
    resp = client.get('/api/v1/admin/aws-secrets')
    assert resp.status_code == 409
    assert resp.get_json()['configured'] is False


def test_central_list_requires_admin(client, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    for username in ('dev', 'ops'):
        login(client, username)
        assert client.get('/api/v1/admin/aws-secrets').status_code == 403
        client.post('/api/v1/auth/logout')


# --- central list + mapping annotation -------------------------------------

def test_central_list_returns_live_secrets_with_mappings(
        client, project, aws_configured, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    _associate(client, project)

    body = client.get('/api/v1/admin/aws-secrets').get_json()
    assert len(body['aws_secrets']) == 1
    row = body['aws_secrets'][0]
    assert row['aws_arn'] == ARN
    assert row['aws_region'] == 'us-east-1'  # parsed from the ARN
    assert len(row['mappings']) == 1
    assert row['mappings'][0]['project_id'] == project.id


# --- associate / dissociate ------------------------------------------------

def test_associate_derives_key_and_region(client, project, aws_configured, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    resp = _associate(client, project)
    assert resp.status_code == 201, resp.get_json()
    row = resp.get_json()
    assert row['key'] == 'DB_PASSWORD'          # last path segment of the name
    assert row['aws_region'] == 'us-east-1'      # parsed from the ARN
    assert row['aws'] is True
    assert 'value' not in row


def test_duplicate_association_is_rejected(client, project, aws_configured, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    assert _associate(client, project).status_code == 201
    dup = _associate(client, project)
    assert dup.status_code == 400
    assert 'already mapped' in dup.get_json()['error']


def test_same_secret_allowed_in_different_environment_scopes(
        client, project, aws_configured, monkeypatch):
    _stub_aws(monkeypatch)
    env = project.environments.first()
    login(client, 'admin')
    a = _associate(client, project)
    b = _associate(client, project, environment_id=env.id)
    assert a.status_code == 201 and b.status_code == 201


def test_associate_foreign_environment_is_rejected(
        client, project, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    other = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                    created_by=users['admin'].id)
    db.session.add(other)
    db.session.flush()
    from app.models.environment import Environment
    foreign = Environment(project_id=other.id, name='dev', display_name='Dev')
    db.session.add(foreign)
    db.session.commit()

    login(client, 'admin')
    resp = _associate(client, project, environment_id=foreign.id)
    assert resp.status_code == 400
    assert 'does not belong' in resp.get_json()['error']


def test_dissociate_removes_mapping(client, project, aws_configured, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    aid = _associate(client, project).get_json()['id']
    resp = client.delete(f'/api/v1/admin/projects/{project.id}/aws-secrets/{aid}')
    assert resp.status_code == 200
    assert ProjectAwsSecret.query.count() == 0


# --- developer reveal (live) -----------------------------------------------

def test_member_with_permission_reveals_live_value(
        client, project, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    aid = _associate(client, project).get_json()['id']
    client.post('/api/v1/auth/logout')

    _grant(project, users['dev'], True)
    login(client, 'dev')
    resp = client.post(f'/api/v1/projects/{project.id}/aws-secrets/{aid}/reveal')
    assert resp.status_code == 200
    assert resp.get_json()['value'] == VALUE


def test_member_without_permission_cannot_reveal(
        client, project, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    aid = _associate(client, project).get_json()['id']
    client.post('/api/v1/auth/logout')

    _grant(project, users['dev'], False)
    login(client, 'dev')
    resp = client.post(f'/api/v1/projects/{project.id}/aws-secrets/{aid}/reveal')
    assert resp.status_code == 403
    assert VALUE not in resp.get_data(as_text=True)


def test_list_never_returns_values(client, project, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    _associate(client, project)
    body = client.get(f'/api/v1/projects/{project.id}/aws-secrets').get_data(as_text=True)
    assert VALUE not in body


def test_reveal_is_audited_even_when_aws_fails(
        client, project, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    aid = _associate(client, project).get_json()['id']
    client.post('/api/v1/auth/logout')

    # AWS now fails on fetch — the reveal attempt must still be audited.
    def boom(self, secret_id, region=None):
        raise RuntimeError('AccessDenied: secretsmanager:GetSecretValue')
    monkeypatch.setattr(aws_manager.AWSManager, 'get_secret_string', boom)

    _grant(project, users['dev'], True)
    login(client, 'dev')
    resp = client.post(f'/api/v1/projects/{project.id}/aws-secrets/{aid}/reveal')
    assert resp.status_code == 502
    assert 'Could not read the secret from AWS' in resp.get_json()['error']

    entry = AuditLog.query.filter_by(action='aws_secret_revealed').first()
    assert entry is not None
    assert entry.details['project_id'] == project.id
    assert VALUE not in str(entry.details)


# --- admin reveal on the central page --------------------------------------

def test_admin_reveal_returns_value(client, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    resp = client.post('/api/v1/admin/aws-secrets/reveal', json={'aws_arn': ARN})
    assert resp.status_code == 200
    assert resp.get_json()['value'] == VALUE


# --- settings status -------------------------------------------------------

def test_status_reports_aws_reachable(client, aws_configured, users, monkeypatch):
    _stub_aws(monkeypatch)
    login(client, 'admin')
    aws = client.get('/api/v1/admin/settings/status').get_json()['aws']
    assert aws['configured'] is True
    assert aws['reachable'] is True
    assert aws['secret_count'] == 1


def test_status_reports_aws_unreachable(client, aws_configured, users, monkeypatch):
    def boom(self, region=None):
        raise RuntimeError('InvalidSignatureException')
    monkeypatch.setattr(aws_manager.AWSManager, 'list_all_secrets', boom)
    login(client, 'admin')
    aws = client.get('/api/v1/admin/settings/status').get_json()['aws']
    assert aws['configured'] is True
    assert aws['reachable'] is False
    assert aws['error']


def test_status_aws_not_configured_when_no_creds(client, users):
    login(client, 'admin')
    aws = client.get('/api/v1/admin/settings/status').get_json()['aws']
    assert aws['configured'] is False
    assert aws['reachable'] is False


# --- detail page -----------------------------------------------------------

def _stub_describe(monkeypatch, description='db', last_changed=None):
    monkeypatch.setattr(
        aws_manager.AWSManager, 'describe_secret',
        lambda self, secret_id, region=None: {
            'name': 'demo/DB_PASSWORD', 'arn': ARN,
            'description': description, 'last_changed': last_changed})


def test_detail_returns_metadata_and_mappings(
        client, project, aws_configured, monkeypatch):
    _stub_aws(monkeypatch)
    _stub_describe(monkeypatch)
    login(client, 'admin')
    _associate(client, project)

    resp = client.get(f'/api/v1/admin/aws-secrets/detail?arn={ARN}')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['aws_name'] == 'demo/DB_PASSWORD'
    assert body['aws_region'] == 'us-east-1'
    assert 'value' not in body
    assert len(body['mappings']) == 1
    assert body['mappings'][0]['project_id'] == project.id


def test_detail_requires_admin(client, aws_configured, users, monkeypatch):
    _stub_describe(monkeypatch)
    for username in ('dev', 'ops'):
        login(client, username)
        assert client.get(f'/api/v1/admin/aws-secrets/detail?arn={ARN}').status_code == 403
        client.post('/api/v1/auth/logout')


# --- create ----------------------------------------------------------------

def test_create_secret_calls_aws_and_audits(client, aws_configured, users, monkeypatch):
    seen = {}

    def fake_create(self, name, value, description=None, region=None):
        seen.update(name=name, value=value, description=description, region=region)
        return {'name': name, 'arn': ARN}
    monkeypatch.setattr(aws_manager.AWSManager, 'create_secret', fake_create)

    login(client, 'admin')
    resp = client.post('/api/v1/admin/aws-secrets',
                       json={'name': 'demo/NEW', 'value': 'sekret', 'description': 'x'})
    assert resp.status_code == 201, resp.get_json()
    assert seen['name'] == 'demo/NEW' and seen['value'] == 'sekret'
    assert 'sekret' not in resp.get_data(as_text=True)

    entry = AuditLog.query.filter_by(action='aws_secret_created').first()
    assert entry is not None
    assert 'sekret' not in str(entry.details)


def test_create_requires_name_and_value(client, aws_configured, users, monkeypatch):
    monkeypatch.setattr(aws_manager.AWSManager, 'create_secret',
                        lambda self, name, value, description=None, region=None: {})
    login(client, 'admin')
    assert client.post('/api/v1/admin/aws-secrets',
                       json={'name': '', 'value': 'v'}).status_code == 400
    assert client.post('/api/v1/admin/aws-secrets',
                       json={'name': 'n', 'value': ''}).status_code == 400


def test_create_aws_failure_is_502(client, aws_configured, users, monkeypatch):
    def boom(self, name, value, description=None, region=None):
        raise RuntimeError('ResourceExistsException')
    monkeypatch.setattr(aws_manager.AWSManager, 'create_secret', boom)
    login(client, 'admin')
    resp = client.post('/api/v1/admin/aws-secrets', json={'name': 'x', 'value': 'y'})
    assert resp.status_code == 502
    assert 'Could not create' in resp.get_json()['error']


# --- edit value / description ----------------------------------------------

def test_edit_value_calls_put(client, aws_configured, users, monkeypatch):
    calls = []
    monkeypatch.setattr(aws_manager.AWSManager, 'put_secret_value',
                        lambda self, sid, value, region=None: calls.append(value))
    _stub_describe(monkeypatch)
    login(client, 'admin')
    resp = client.put('/api/v1/admin/aws-secrets', json={'aws_arn': ARN, 'value': 'v2'})
    assert resp.status_code == 200
    assert calls == ['v2']

    entry = AuditLog.query.filter_by(action='aws_secret_value_updated').first()
    assert entry is not None
    assert 'v2' not in str(entry.details)


def test_edit_blank_value_does_not_call_put(client, aws_configured, users, monkeypatch):
    calls = []
    monkeypatch.setattr(aws_manager.AWSManager, 'put_secret_value',
                        lambda self, sid, value, region=None: calls.append(value))
    upd = []
    monkeypatch.setattr(aws_manager.AWSManager, 'update_secret_description',
                        lambda self, sid, desc, region=None: upd.append(desc))
    _stub_describe(monkeypatch)
    login(client, 'admin')
    # Only a description change; value left blank.
    resp = client.put('/api/v1/admin/aws-secrets',
                      json={'aws_arn': ARN, 'value': '', 'description': 'new desc'})
    assert resp.status_code == 200
    assert calls == []
    assert upd == ['new desc']


def test_update_requires_something(client, aws_configured, users, monkeypatch):
    _stub_describe(monkeypatch)
    login(client, 'admin')
    resp = client.put('/api/v1/admin/aws-secrets', json={'aws_arn': ARN})
    assert resp.status_code == 400


def test_write_endpoints_require_admin(client, aws_configured, users, monkeypatch):
    for username in ('dev', 'ops'):
        login(client, username)
        assert client.post('/api/v1/admin/aws-secrets',
                           json={'name': 'n', 'value': 'v'}).status_code == 403
        assert client.put('/api/v1/admin/aws-secrets',
                          json={'aws_arn': ARN, 'value': 'v'}).status_code == 403
        client.post('/api/v1/auth/logout')
