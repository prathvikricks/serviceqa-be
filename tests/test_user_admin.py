"""Admin user delete — guarded so users with activity can't be silently wiped."""
from app.extensions import db
from app.models.request import EnvironmentRequest
from app.models.user import ProjectMember, User

from conftest import login, make_user


def test_delete_clean_user(client, users):
    victim = make_user('throwaway', 'developer')
    login(client, 'admin')
    resp = client.delete(f'/api/v1/admin/users/{victim.id}')
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['deleted'] is True
    assert db.session.get(User, victim.id) is None


def test_delete_cleans_up_membership(client, project, users):
    # 'dev' is a member of the demo project (conftest.project). No requests yet.
    dev_id = users['dev'].id
    assert ProjectMember.query.filter_by(user_id=dev_id).count() >= 1
    login(client, 'admin')
    resp = client.delete(f'/api/v1/admin/users/{dev_id}')
    assert resp.status_code == 200, resp.get_json()
    assert db.session.get(User, dev_id) is None
    # The membership row is gone, not orphaned.
    assert ProjectMember.query.filter_by(user_id=dev_id).count() == 0


def test_cannot_delete_self(client, users):
    login(client, 'admin')
    resp = client.delete(f"/api/v1/admin/users/{users['admin'].id}")
    assert resp.status_code == 400
    assert 'your own account' in resp.get_json()['error']


def test_admin_can_delete_another_admin_when_not_last(client, users):
    # With two admins, one can delete the other; the last-admin guard only bites
    # when it would leave zero admins (and self-delete is separately blocked).
    make_user('admin2', 'admin')
    login(client, 'admin2')
    resp = client.delete(f"/api/v1/admin/users/{users['admin'].id}")
    assert resp.status_code == 200
    assert db.session.get(User, users['admin'].id) is None


def test_delete_user_with_activity_is_blocked(client, project, users):
    victim = make_user('busy', 'developer')
    req = EnvironmentRequest(requester_id=victim.id,
                             environment_id=project.environments.first().id,
                             action_type='start_stop', status='pending',
                             reason='need it for testing')
    db.session.add(req)
    db.session.commit()

    login(client, 'admin')
    resp = client.delete(f'/api/v1/admin/users/{victim.id}')
    assert resp.status_code == 400
    assert 'Deactivate' in resp.get_json()['error']
    assert db.session.get(User, victim.id) is not None  # still there


def test_delete_requires_admin(client, users):
    victim = make_user('victim2', 'developer')
    for username in ('dev', 'ops'):
        login(client, username)
        assert client.delete(f'/api/v1/admin/users/{victim.id}').status_code == 403
        client.post('/api/v1/auth/logout')
    assert db.session.get(User, victim.id) is not None


def test_delete_is_audited_without_sensitive_data(client, users):
    from app.models.audit import AuditLog
    victim = make_user('audit-me', 'developer')
    vid = victim.id
    login(client, 'admin')
    client.delete(f'/api/v1/admin/users/{vid}')

    entry = AuditLog.query.filter_by(action='user_deleted').first()
    assert entry is not None
    assert entry.entity_id == vid
    assert entry.details.get('username') == 'audit-me'
