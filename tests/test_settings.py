"""Runtime settings: admin-only, encrypted, and never echoed back."""
import json

from app.extensions import db
from app.models.setting import Setting, get_setting
from app.services import chat_agent

from conftest import login

KEY = 'GEMINI_API_KEY'


def test_only_an_admin_can_read_settings(client, users):
    for username, expected in (('dev', 403), ('ops', 403), ('admin', 200)):
        client.post('/api/v1/auth/logout')
        login(client, username)
        assert client.get('/api/v1/admin/settings').status_code == expected


def test_setting_a_key_never_returns_it(client, users):
    login(client, 'admin')
    resp = client.put(f'/api/v1/admin/settings/{KEY}',
                      json={'value': 'AIzaSy-super-secret-value-1234'})
    assert resp.status_code == 200
    body = resp.get_json()

    assert 'super-secret' not in json.dumps(body)
    assert body['is_set'] is True
    assert body['source'] == 'settings'
    # Enough to tell two keys apart, not enough to be useful.
    assert body['hint'] == '…1234'


def test_the_stored_value_is_encrypted_at_rest(client, users):
    login(client, 'admin')
    client.put(f'/api/v1/admin/settings/{KEY}', json={'value': 'plaintext-key'})

    row = Setting.query.filter_by(key=KEY).one()
    assert 'plaintext-key' not in row.value
    assert row.get_value() == 'plaintext-key'


def test_a_saved_key_enables_the_assistant_without_a_restart(client, app, users):
    app.config['GEMINI_API_KEY'] = None
    assert chat_agent.is_enabled() is False

    login(client, 'admin')
    client.put(f'/api/v1/admin/settings/{KEY}', json={'value': 'a-real-key'})

    assert chat_agent.is_enabled() is True
    assert chat_agent.api_key() == 'a-real-key'


def test_settings_override_the_environment(client, app, users):
    app.config['GEMINI_API_KEY'] = 'from-env'
    login(client, 'admin')
    client.put(f'/api/v1/admin/settings/{KEY}', json={'value': 'from-settings'})

    assert get_setting(KEY) == 'from-settings'


def test_clearing_falls_back_to_the_environment(client, app, users):
    app.config['GEMINI_API_KEY'] = 'from-env'
    login(client, 'admin')
    client.put(f'/api/v1/admin/settings/{KEY}', json={'value': 'from-settings'})

    body = client.delete(f'/api/v1/admin/settings/{KEY}').get_json()
    assert get_setting(KEY) == 'from-env'
    assert body['source'] == 'environment'


def test_an_unknown_key_cannot_be_written(client, users):
    login(client, 'admin')
    assert client.put('/api/v1/admin/settings/SECRET_KEY',
                      json={'value': 'pwned'}).status_code == 400
    assert client.delete('/api/v1/admin/settings/SECRET_KEY').status_code == 400


def test_an_empty_value_is_rejected(client, users):
    login(client, 'admin')
    assert client.put(f'/api/v1/admin/settings/{KEY}',
                      json={'value': '   '}).status_code == 400


def test_a_developer_cannot_write_a_setting(client, users):
    login(client, 'dev')
    assert client.put(f'/api/v1/admin/settings/{KEY}',
                      json={'value': 'nope'}).status_code == 403


def test_the_audit_entry_carries_no_value(client, users):
    from app.models.audit import AuditLog

    login(client, 'admin')
    client.put(f'/api/v1/admin/settings/{KEY}', json={'value': 'do-not-log-me'})

    entry = AuditLog.query.filter_by(action='setting_updated').one()
    assert 'do-not-log-me' not in json.dumps(entry.details)
    assert entry.details['key'] == KEY


def test_a_broken_ciphertext_reads_as_unset(client, app, users):
    """A rotated CRED_KEY must degrade, not 500 the settings page."""
    app.config['GEMINI_API_KEY'] = None
    row = Setting(key=KEY, value='not-a-valid-fernet-token')
    db.session.add(row)
    db.session.commit()

    assert row.get_value() == ''
    assert get_setting(KEY) is None

    login(client, 'admin')
    assert client.get('/api/v1/admin/settings').status_code == 200


def test_graph_credentials_can_be_set_from_the_ui(client, app, users):
    """Setup should not need a shell and a container restart."""
    from app.services import graph_mail

    app.config.update(GRAPH_TENANT_ID=None, GRAPH_CLIENT_ID=None,
                      GRAPH_CLIENT_SECRET=None, DEVOPS_MAILBOX=None)
    assert graph_mail.is_enabled() is False

    login(client, 'admin')
    for key, value in (('GRAPH_TENANT_ID', 't-1'), ('GRAPH_CLIENT_ID', 'c-1'),
                       ('GRAPH_CLIENT_SECRET', 's-1'),
                       ('DEVOPS_MAILBOX', 'devops@pacewisdom.com')):
        assert client.put(f'/api/v1/admin/settings/{key}',
                          json={'value': value}).status_code == 200

    assert graph_mail.is_enabled() is True


def test_the_client_secret_is_never_echoed(client, users):
    login(client, 'admin')
    client.put('/api/v1/admin/settings/GRAPH_CLIENT_SECRET',
               json={'value': 'Xy8Q~verysecretvalue'})
    body = client.get('/api/v1/admin/settings').get_json()
    assert 'verysecretvalue' not in json.dumps(body)


def test_the_tenant_id_is_shown_in_full(client, users):
    """Non-secret settings are more useful visible than masked."""
    login(client, 'admin')
    client.put('/api/v1/admin/settings/GRAPH_TENANT_ID', json={'value': 'contoso-tenant'})
    body = client.get('/api/v1/admin/settings').get_json()
    tenant = next(s for s in body['settings'] if s['key'] == 'GRAPH_TENANT_ID')
    assert tenant['hint'] == 'contoso-tenant'


def test_the_ack_toggle_treats_zero_as_off(client, app, users):
    """A stored '0' is a truthy string — reading it naively would mail people."""
    from app.models.setting import setting_bool

    login(client, 'admin')
    client.put('/api/v1/admin/settings/TICKET_ACK_ENABLED', json={'value': '0'})
    assert setting_bool('TICKET_ACK_ENABLED') is False

    client.put('/api/v1/admin/settings/TICKET_ACK_ENABLED', json={'value': '1'})
    assert setting_bool('TICKET_ACK_ENABLED') is True


def test_changing_a_graph_credential_drops_the_cached_token(client, app, users, monkeypatch):
    from app.services import graph_mail

    dropped = []
    monkeypatch.setattr(graph_mail, 'reset_token_cache', lambda: dropped.append(1))

    login(client, 'admin')
    client.put('/api/v1/admin/settings/GRAPH_CLIENT_SECRET', json={'value': 'rotated'})
    assert dropped, 'a rotated secret must not keep working on the old token'
