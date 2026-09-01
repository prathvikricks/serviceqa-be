"""Shared secret catalog: central CRUD, attach/detach to projects, and the
one-source-of-truth reveal path scoped through project membership."""
import pytest

from app.extensions import db
from app.models.audit import AuditLog
from app.models.project import Project
from app.models.shared_secret import SharedSecret, SharedSecretAttachment
from app.models.user import ProjectMember

from conftest import login, make_user

PLAINTEXT = 'shared-db-password'


@pytest.fixture
def shared(app, users):
    """A catalog secret created directly, bypassing the API."""
    s = SharedSecret(key='SHARED_DB', description='shared db',
                     created_by=users['admin'].id)
    s.set_value(PLAINTEXT)
    db.session.add(s)
    db.session.commit()
    return s


def _grant(project, user, can_view):
    member = ProjectMember.query.filter_by(
        project_id=project.id, user_id=user.id).first()
    if member is None:
        member = ProjectMember(project_id=project.id, user_id=user.id, added_by=user.id)
        db.session.add(member)
    member.can_view_secrets = can_view
    db.session.commit()
    return member


def _attach(client, project, shared_id, environment_id=None):
    return client.post(f'/api/v1/admin/projects/{project.id}/shared-secrets',
                       json={'shared_secret_id': shared_id,
                             'environment_id': environment_id})


# --- storage ---------------------------------------------------------------

def test_value_is_encrypted_at_rest(app, shared):
    row = db.session.execute(
        db.text('SELECT value FROM shared_secrets WHERE id = :i'), {'i': shared.id}
    ).scalar()
    assert PLAINTEXT not in row
    assert shared.get_value() == PLAINTEXT


# --- catalog CRUD ----------------------------------------------------------

def test_create_requires_admin(client, users):
    for username in ('dev', 'ops'):
        login(client, username)
        resp = client.post('/api/v1/admin/shared-secrets',
                           json={'key': 'X', 'value': 'y'})
        assert resp.status_code == 403, f'{username} could create'
        client.post('/api/v1/auth/logout')


def test_create_and_list_never_returns_value(client, users):
    login(client, 'admin')
    resp = client.post('/api/v1/admin/shared-secrets',
                       json={'key': 'API_KEY', 'value': 'sk_live', 'description': 'x'})
    assert resp.status_code == 201, resp.get_json()
    assert 'sk_live' not in resp.get_data(as_text=True)

    listing = client.get('/api/v1/admin/shared-secrets').get_json()
    assert listing['shared_secrets'][0]['key'] == 'API_KEY'
    assert 'value' not in listing['shared_secrets'][0]
    assert listing['shared_secrets'][0]['attachment_count'] == 0


def test_duplicate_key_is_rejected(client, shared, users):
    login(client, 'admin')
    resp = client.post('/api/v1/admin/shared-secrets',
                       json={'key': 'SHARED_DB', 'value': 'other'})
    assert resp.status_code == 400
    assert 'already exists' in resp.get_json()['error']


def test_blank_value_on_edit_keeps_the_stored_one(client, shared, users):
    login(client, 'admin')
    resp = client.put(f'/api/v1/admin/shared-secrets/{shared.id}',
                      json={'key': 'SHARED_DB', 'value': '', 'description': 'renamed'})
    assert resp.status_code == 200
    db.session.expire_all()
    assert db.session.get(SharedSecret, shared.id).get_value() == PLAINTEXT


# --- attach / detach -------------------------------------------------------

def test_attach_then_appears_for_project_members(client, project, shared, users):
    login(client, 'admin')
    resp = _attach(client, project, shared.id)
    assert resp.status_code == 201, resp.get_json()

    listing = client.get(f'/api/v1/projects/{project.id}/shared-secrets').get_json()
    assert len(listing['shared_secrets']) == 1
    row = listing['shared_secrets'][0]
    assert row['key'] == 'SHARED_DB'
    assert row['shared'] is True
    assert 'value' not in row


def test_attaching_twice_to_same_scope_is_rejected(client, project, shared, users):
    login(client, 'admin')
    assert _attach(client, project, shared.id).status_code == 201
    dup = _attach(client, project, shared.id)
    assert dup.status_code == 400
    assert 'already attached' in dup.get_json()['error']


def test_attach_to_foreign_environment_is_rejected(client, project, shared, users):
    other = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                    created_by=users['admin'].id)
    db.session.add(other)
    db.session.flush()
    from app.models.environment import Environment
    foreign_env = Environment(project_id=other.id, name='dev', display_name='Dev')
    db.session.add(foreign_env)
    db.session.commit()

    login(client, 'admin')
    resp = _attach(client, project, shared.id, environment_id=foreign_env.id)
    assert resp.status_code == 400
    assert 'does not belong' in resp.get_json()['error']


def test_detach_leaves_catalog_secret_intact(client, project, shared, users):
    login(client, 'admin')
    aid = _attach(client, project, shared.id).get_json()['id']

    resp = client.delete(f'/api/v1/admin/projects/{project.id}/shared-secrets/{aid}')
    assert resp.status_code == 200
    assert SharedSecretAttachment.query.count() == 0
    # The catalog entry survives a detach.
    assert db.session.get(SharedSecret, shared.id) is not None


def test_deleting_catalog_secret_removes_attachments(client, project, shared, users):
    login(client, 'admin')
    _attach(client, project, shared.id)
    assert SharedSecretAttachment.query.count() == 1

    resp = client.delete(f'/api/v1/admin/shared-secrets/{shared.id}')
    assert resp.status_code == 200
    assert SharedSecretAttachment.query.count() == 0


# --- reveal auth (through a project) ---------------------------------------

def test_member_with_permission_can_reveal_shared(client, project, shared, users):
    login(client, 'admin')
    aid = _attach(client, project, shared.id).get_json()['id']
    client.post('/api/v1/auth/logout')

    _grant(project, users['dev'], True)
    login(client, 'dev')
    resp = client.post(f'/api/v1/projects/{project.id}/shared-secrets/{aid}/reveal')
    assert resp.status_code == 200
    assert resp.get_json()['value'] == PLAINTEXT


def test_member_without_permission_cannot_reveal_shared(client, project, shared, users):
    login(client, 'admin')
    aid = _attach(client, project, shared.id).get_json()['id']
    client.post('/api/v1/auth/logout')

    _grant(project, users['dev'], False)
    login(client, 'dev')
    resp = client.post(f'/api/v1/projects/{project.id}/shared-secrets/{aid}/reveal')
    assert resp.status_code == 403
    assert PLAINTEXT not in resp.get_data(as_text=True)


def test_attachment_of_another_project_is_not_reachable(client, project, shared, users):
    other = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                    created_by=users['admin'].id)
    db.session.add(other)
    db.session.commit()
    login(client, 'admin')
    aid = _attach(client, project, shared.id).get_json()['id']

    # Same attachment id, wrong project in the path.
    resp = client.post(f'/api/v1/projects/{other.id}/shared-secrets/{aid}/reveal')
    assert resp.status_code == 404


# --- one source of truth ---------------------------------------------------

def test_editing_value_updates_every_attached_project(client, users):
    """The whole point: one edit changes the value everywhere it's attached."""
    p1 = Project(name='P1', slug='p1', cloud_provider='aws', mode='mock',
                 created_by=users['admin'].id)
    p2 = Project(name='P2', slug='p2', cloud_provider='aws', mode='mock',
                 created_by=users['admin'].id)
    db.session.add_all([p1, p2])
    db.session.commit()

    login(client, 'admin')
    sid = client.post('/api/v1/admin/shared-secrets',
                      json={'key': 'TOKEN', 'value': 'v1'}).get_json()['id']
    a1 = _attach(client, p1, sid).get_json()['id']
    a2 = _attach(client, p2, sid).get_json()['id']

    # Rotate the value once, centrally.
    client.put(f'/api/v1/admin/shared-secrets/{sid}', json={'value': 'v2'})

    r1 = client.post(f'/api/v1/projects/{p1.id}/shared-secrets/{a1}/reveal').get_json()
    r2 = client.post(f'/api/v1/projects/{p2.id}/shared-secrets/{a2}/reveal').get_json()
    assert r1['value'] == 'v2' and r2['value'] == 'v2'


# --- audit -----------------------------------------------------------------

def test_reveal_is_audited_without_the_value(client, project, shared, users):
    login(client, 'admin')
    aid = _attach(client, project, shared.id).get_json()['id']
    client.post('/api/v1/auth/logout')

    _grant(project, users['dev'], True)
    login(client, 'dev')
    client.post(f'/api/v1/projects/{project.id}/shared-secrets/{aid}/reveal')

    entry = AuditLog.query.filter_by(action='shared_secret_revealed').first()
    assert entry is not None
    assert entry.details['key'] == 'SHARED_DB'
    assert PLAINTEXT not in str(entry.details)
