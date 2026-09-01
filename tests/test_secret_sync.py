"""AWS Secrets Manager sync into project secrets.

The AWS calls are stubbed (see `_stub_aws`) so no boto3/network is involved —
the tests exercise the upsert, permission, and audit logic, not AWS itself.
"""
import pytest

from app.extensions import db
from app.models.audit import AuditLog
from app.models.secret import ProjectSecret
from app.services import aws_manager

from conftest import login


@pytest.fixture
def real_aws_project(project):
    """The shared AWS project, flipped to real mode so sync is allowed."""
    project.mode = 'real'
    project.set_provider_config({
        'region': 'us-east-1',
        'access_key_id': 'AKIA_TEST',
        'secret_access_key': 'shhh',
    })
    db.session.commit()
    return project


def _stub_aws(monkeypatch, entries, values):
    """Patch AWSManager so no boto3 call is made.

    entries: list of {'name', 'arn', 'description'}. values: arn -> SecretString.
    """
    monkeypatch.setattr(aws_manager.AWSManager, 'list_secrets_by_tag',
                        lambda self, k, v, region=None: entries)
    monkeypatch.setattr(aws_manager.AWSManager, 'get_secret_string',
                        lambda self, arn, region=None: values[arn])


def _url(project):
    return f'/api/v1/admin/projects/{project.id}/secrets/sync'


# --- happy path ------------------------------------------------------------

def test_sync_creates_secrets_from_aws(client, real_aws_project, monkeypatch):
    entries = [
        {'name': 'demo/DB_PASSWORD', 'arn': 'arn:...:DB_PASSWORD', 'description': 'db'},
        {'name': 'API_TOKEN', 'arn': 'arn:...:API_TOKEN', 'description': None},
    ]
    values = {'arn:...:DB_PASSWORD': 'pg-pass', 'arn:...:API_TOKEN': 'tok_abc'}
    _stub_aws(monkeypatch, entries, values)

    login(client, 'admin')
    resp = client.post(_url(real_aws_project))
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['created'] == 2 and body['updated'] == 0
    # A synced value must never appear in the sync response.
    assert 'pg-pass' not in resp.get_data(as_text=True)

    # Keyed by the last path segment, encrypted at rest, marked as AWS-sourced.
    rows = ProjectSecret.query.filter_by(project_id=real_aws_project.id).all()
    assert {r.key for r in rows} == {'DB_PASSWORD', 'API_TOKEN'}
    db_secret = next(r for r in rows if r.key == 'DB_PASSWORD')
    assert db_secret.source == 'aws'
    assert db_secret.get_value() == 'pg-pass'
    assert db_secret.synced_at is not None
    assert db_secret.external_id == 'arn:...:DB_PASSWORD'


def test_resync_updates_existing_aws_secret(client, real_aws_project, monkeypatch):
    arn = 'arn:...:DB_PASSWORD'
    login(client, 'admin')

    _stub_aws(monkeypatch, [{'name': 'DB_PASSWORD', 'arn': arn, 'description': 'db'}],
              {arn: 'v1'})
    client.post(_url(real_aws_project))

    # AWS value rotates; re-sync should update in place, not duplicate.
    _stub_aws(monkeypatch, [{'name': 'DB_PASSWORD', 'arn': arn, 'description': 'db'}],
              {arn: 'v2'})
    body = client.post(_url(real_aws_project)).get_json()
    assert body['created'] == 0 and body['updated'] == 1

    rows = ProjectSecret.query.filter_by(
        project_id=real_aws_project.id, key='DB_PASSWORD').all()
    assert len(rows) == 1
    assert rows[0].get_value() == 'v2'


def test_sync_reports_secrets_removed_from_aws(client, real_aws_project, monkeypatch):
    arn = 'arn:...:DB_PASSWORD'
    login(client, 'admin')
    _stub_aws(monkeypatch, [{'name': 'DB_PASSWORD', 'arn': arn, 'description': 'db'}],
              {arn: 'v1'})
    client.post(_url(real_aws_project))

    # Next sync returns nothing — the previously-synced row is now stale.
    _stub_aws(monkeypatch, [], {})
    body = client.post(_url(real_aws_project)).get_json()
    assert body['created'] == 0 and body['updated'] == 0
    assert body['missing_in_aws'] == 1
    # Not deleted — left for an admin to decide.
    assert ProjectSecret.query.filter_by(
        project_id=real_aws_project.id, key='DB_PASSWORD').count() == 1


# --- safety: never clobber a manual secret ---------------------------------

def test_sync_skips_manual_secret_with_same_key(client, real_aws_project, users, monkeypatch):
    manual = ProjectSecret(project_id=real_aws_project.id, key='DB_PASSWORD',
                           source='manual', created_by=users['admin'].id)
    manual.set_value('human-set')
    db.session.add(manual)
    db.session.commit()

    arn = 'arn:...:DB_PASSWORD'
    _stub_aws(monkeypatch, [{'name': 'DB_PASSWORD', 'arn': arn, 'description': 'db'}],
              {arn: 'aws-set'})
    login(client, 'admin')
    body = client.post(_url(real_aws_project)).get_json()
    assert body['created'] == 0 and body['updated'] == 0
    assert [s['key'] for s in body['skipped']] == ['DB_PASSWORD']

    db.session.expire_all()
    assert db.session.get(ProjectSecret, manual.id).get_value() == 'human-set'


# --- guards ----------------------------------------------------------------

def test_sync_requires_real_mode(client, project):
    # The shared fixture is in mock mode.
    login(client, 'admin')
    resp = client.post(_url(project))
    assert resp.status_code == 400
    assert 'Real mode' in resp.get_json()['error']


def test_sync_requires_admin(client, real_aws_project):
    for username in ('dev', 'ops'):
        login(client, username)
        assert client.post(_url(real_aws_project)).status_code == 403, \
            f'{username} could sync'
        client.post('/api/v1/auth/logout')


def test_sync_reports_aws_read_failure(client, real_aws_project, monkeypatch):
    def boom(self, k, v, region=None):
        raise RuntimeError('AccessDenied: secretsmanager:ListSecrets')
    monkeypatch.setattr(aws_manager.AWSManager, 'list_secrets_by_tag', boom)

    login(client, 'admin')
    resp = client.post(_url(real_aws_project))
    assert resp.status_code == 502
    assert 'Could not read AWS Secrets Manager' in resp.get_json()['error']


# --- audit -----------------------------------------------------------------

def test_sync_is_audited_without_recording_values(client, real_aws_project, monkeypatch):
    arn = 'arn:...:DB_PASSWORD'
    _stub_aws(monkeypatch, [{'name': 'DB_PASSWORD', 'arn': arn, 'description': 'db'}],
              {arn: 'pg-pass'})
    login(client, 'admin')
    client.post(_url(real_aws_project))

    entry = AuditLog.query.filter_by(action='secrets_synced').first()
    assert entry is not None
    assert entry.details['created'] == 1
    assert entry.details['tag'] == 'Project=demo'
    assert 'pg-pass' not in str(entry.details)
