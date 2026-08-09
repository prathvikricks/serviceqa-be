"""Project secrets: encryption at rest, and who may reveal a value."""
import pytest

from app.extensions import db
from app.models.audit import AuditLog
from app.models.environment import Environment
from app.models.project import Project
from app.models.secret import ProjectSecret
from app.models.user import ProjectMember

from conftest import login, make_user

PLAINTEXT = 'sup3r-s3cret-db-password'


@pytest.fixture
def secret(app, project, users):
    """A project-wide secret created directly, bypassing the API."""
    s = ProjectSecret(project_id=project.id, key='DB_PASSWORD',
                      description='UAT database', created_by=users['admin'].id)
    s.set_value(PLAINTEXT)
    db.session.add(s)
    db.session.commit()
    return s


def _grant(project, user, can_view):
    """Set (or create) this user's membership permission."""
    member = ProjectMember.query.filter_by(
        project_id=project.id, user_id=user.id).first()
    if member is None:
        member = ProjectMember(project_id=project.id, user_id=user.id, added_by=user.id)
        db.session.add(member)
    member.can_view_secrets = can_view
    db.session.commit()
    return member


# --- storage ---------------------------------------------------------------

def test_value_is_encrypted_at_rest_and_round_trips(app, secret):
    row = db.session.execute(
        db.text('SELECT value FROM project_secrets WHERE id = :i'), {'i': secret.id}
    ).scalar()
    assert PLAINTEXT not in row
    assert row != PLAINTEXT
    assert secret.get_value() == PLAINTEXT


def test_list_endpoint_never_returns_values(client, project, secret, users):
    login(client, 'admin')
    resp = client.get(f'/api/v1/projects/{project.id}/secrets')
    assert resp.status_code == 200
    assert PLAINTEXT not in resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['secrets'][0]['key'] == 'DB_PASSWORD'
    assert 'value' not in body['secrets'][0]


# --- who may reveal --------------------------------------------------------

def test_admin_and_devops_can_always_reveal(client, project, secret, users):
    for username in ('admin', 'ops'):
        login(client, username)
        resp = client.post(f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal')
        assert resp.status_code == 200, f'{username} was denied'
        assert resp.get_json()['value'] == PLAINTEXT
        client.post('/api/v1/auth/logout')


def test_member_without_permission_cannot_reveal(client, project, secret, users):
    _grant(project, users['dev'], False)
    login(client, 'dev')

    # They can see that the secret exists…
    body = client.get(f'/api/v1/projects/{project.id}/secrets').get_json()
    assert body['can_reveal'] is False
    assert body['secrets'][0]['can_reveal'] is False

    # …but not read it.
    resp = client.post(f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal')
    assert resp.status_code == 403
    assert PLAINTEXT not in resp.get_data(as_text=True)


def test_member_with_permission_can_reveal(client, project, secret, users):
    _grant(project, users['dev'], True)
    login(client, 'dev')

    body = client.get(f'/api/v1/projects/{project.id}/secrets').get_json()
    assert body['can_reveal'] is True

    resp = client.post(f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal')
    assert resp.status_code == 200
    assert resp.get_json()['value'] == PLAINTEXT


def test_non_member_is_denied_both_list_and_reveal(client, project, secret):
    make_user('outsider', 'developer')
    login(client, 'outsider')
    assert client.get(f'/api/v1/projects/{project.id}/secrets').status_code == 403
    resp = client.post(f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal')
    assert resp.status_code == 403
    assert PLAINTEXT not in resp.get_data(as_text=True)


def test_revoking_permission_takes_effect_immediately(client, project, secret, users):
    _grant(project, users['dev'], True)
    login(client, 'dev')
    assert client.post(
        f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal').status_code == 200

    _grant(project, users['dev'], False)
    assert client.post(
        f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal').status_code == 403


def test_admin_toggles_permission_through_the_api(client, project, secret, users):
    member = _grant(project, users['dev'], False)
    login(client, 'admin')
    resp = client.put(f'/api/v1/admin/projects/{project.id}/members/{member.id}',
                      json={'can_view_secrets': True})
    assert resp.status_code == 200
    assert resp.get_json()['can_view_secrets'] is True
    client.post('/api/v1/auth/logout')

    login(client, 'dev')
    assert client.post(
        f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal').status_code == 200


# --- audit -----------------------------------------------------------------

def test_reveal_is_audited_without_recording_the_value(client, project, secret, users):
    _grant(project, users['dev'], True)
    login(client, 'dev')
    client.post(f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal')

    entry = AuditLog.query.filter_by(action='secret_revealed').first()
    assert entry is not None
    assert entry.user_id == users['dev'].id
    assert entry.entity_id == secret.id
    assert entry.details['key'] == 'DB_PASSWORD'
    # The whole point: the audit trail records the access, never the secret.
    assert PLAINTEXT not in str(entry.details)


def test_denied_reveal_is_not_audited_as_a_reveal(client, project, secret, users):
    _grant(project, users['dev'], False)
    login(client, 'dev')
    client.post(f'/api/v1/projects/{project.id}/secrets/{secret.id}/reveal')
    assert AuditLog.query.filter_by(action='secret_revealed').count() == 0


# --- admin CRUD ------------------------------------------------------------

def test_create_requires_admin(client, project, users):
    for username in ('dev', 'ops'):
        login(client, username)
        resp = client.post(f'/api/v1/admin/projects/{project.id}/secrets',
                           json={'key': 'X', 'value': 'y'})
        assert resp.status_code == 403, f'{username} could create a secret'
        client.post('/api/v1/auth/logout')


def test_create_and_reveal_round_trip(client, project, users):
    login(client, 'admin')
    resp = client.post(f'/api/v1/admin/projects/{project.id}/secrets',
                       json={'key': 'API_TOKEN', 'value': 'tok_123', 'description': 'CI'})
    assert resp.status_code == 201, resp.get_json()
    sid = resp.get_json()['id']
    assert 'tok_123' not in resp.get_data(as_text=True)

    revealed = client.post(f'/api/v1/projects/{project.id}/secrets/{sid}/reveal')
    assert revealed.get_json()['value'] == 'tok_123'


def test_blank_value_on_edit_keeps_the_stored_one(client, project, secret, users):
    login(client, 'admin')
    resp = client.put(f'/api/v1/admin/projects/{project.id}/secrets/{secret.id}',
                      json={'key': 'DB_PASSWORD', 'value': '', 'description': 'renamed'})
    assert resp.status_code == 200
    db.session.expire_all()
    assert db.session.get(ProjectSecret, secret.id).get_value() == PLAINTEXT


def test_duplicate_key_in_the_same_scope_is_rejected(client, project, secret, users):
    login(client, 'admin')
    resp = client.post(f'/api/v1/admin/projects/{project.id}/secrets',
                       json={'key': 'DB_PASSWORD', 'value': 'other'})
    assert resp.status_code == 400
    assert 'already exists' in resp.get_json()['error']


def test_same_key_is_allowed_in_different_environment_scopes(client, project, users):
    """The point of the environment pin: API_URL @dev vs API_URL @uat."""
    env = project.environments.first()
    login(client, 'admin')
    a = client.post(f'/api/v1/admin/projects/{project.id}/secrets',
                    json={'key': 'API_URL', 'value': 'https://all'})
    b = client.post(f'/api/v1/admin/projects/{project.id}/secrets',
                    json={'key': 'API_URL', 'value': 'https://uat', 'environment_id': env.id})
    assert a.status_code == 201 and b.status_code == 201, (a.get_json(), b.get_json())
    assert a.get_json()['scope'] == 'All environments'
    assert b.get_json()['scope'] == env.display_name


def test_environment_from_another_project_is_rejected(client, project, users):
    other = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                    created_by=users['admin'].id)
    db.session.add(other)
    db.session.flush()
    foreign_env = Environment(project_id=other.id, name='dev', display_name='Dev')
    db.session.add(foreign_env)
    db.session.commit()

    login(client, 'admin')
    resp = client.post(f'/api/v1/admin/projects/{project.id}/secrets',
                       json={'key': 'K', 'value': 'v', 'environment_id': foreign_env.id})
    assert resp.status_code == 400
    assert 'does not belong' in resp.get_json()['error']


def test_secret_of_another_project_is_not_reachable_through_this_one(
        client, project, secret, users):
    other = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                    created_by=users['admin'].id)
    db.session.add(other)
    db.session.commit()

    login(client, 'admin')
    resp = client.post(f'/api/v1/projects/{other.id}/secrets/{secret.id}/reveal')
    assert resp.status_code == 404
    assert PLAINTEXT not in resp.get_data(as_text=True)


def test_deleting_the_project_deletes_its_secrets(client, project, secret, users):
    login(client, 'admin')
    assert client.delete(f'/api/v1/admin/projects/{project.id}').status_code == 200
    assert db.session.get(ProjectSecret, secret.id) is None
