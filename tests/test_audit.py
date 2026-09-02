"""Audit log detail endpoint."""
from app.models.audit import AuditLog

from conftest import login


def test_audit_detail_returns_entry(client, users):
    login(client, 'admin')
    # Logging in wrote a user_login entry; grab one to fetch.
    entry = AuditLog.query.order_by(AuditLog.id.desc()).first()
    assert entry is not None
    resp = client.get(f'/api/v1/admin/audit/{entry.id}')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['id'] == entry.id
    assert body['action'] == entry.action
    assert 'details' in body


def test_audit_detail_404_for_missing(client, users):
    login(client, 'admin')
    assert client.get('/api/v1/admin/audit/999999').status_code == 404


def test_audit_detail_requires_admin(client, users):
    login(client, 'admin')
    entry = AuditLog.query.first()
    client.post('/api/v1/auth/logout')
    for username in ('dev', 'ops'):
        login(client, username)
        assert client.get(f'/api/v1/admin/audit/{entry.id}').status_code == 403
        client.post('/api/v1/auth/logout')
