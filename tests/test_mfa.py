"""Mandatory TOTP MFA: forced first-login enrollment, per-login verification,
and admin reset. AWS/crypto are real here; only the clock (authenticator) is
simulated via pyotp generating the current code."""
import pyotp

from app.extensions import db
from app.models.user import User

from conftest import make_user, login, TEST_TOTP_SECRET


def _unenrolled(username='newdev', role='developer'):
    """A user with a password but no MFA yet (bypasses make_user's enrollment)."""
    from app.models.user import Role
    role_row = Role.query.filter_by(name=role).first()
    u = User(username=username, email=f'{username}@example.com', role_id=role_row.id)
    u.set_password('password123')
    db.session.add(u)
    db.session.commit()
    return u


# --- forced enrollment on first login --------------------------------------

def test_login_without_mfa_requires_setup(client, app):
    _unenrolled()
    resp = client.post('/api/v1/auth/login',
                       json={'username': 'newdev', 'password': 'password123'})
    assert resp.status_code == 200
    assert resp.get_json().get('mfa_setup_required') is True
    # Not logged in yet.
    assert client.get('/api/v1/auth/me').status_code == 401


def test_setup_then_confirm_enrolls_and_logs_in(client, app):
    _unenrolled()
    client.post('/api/v1/auth/login',
                json={'username': 'newdev', 'password': 'password123'})

    setup = client.post('/api/v1/auth/mfa/setup')
    assert setup.status_code == 200
    body = setup.get_json()
    secret = body['secret']
    assert body['otpauth_uri'].startswith('otpauth://totp/')
    assert '<svg' in body['qr_svg']

    code = pyotp.TOTP(secret).now()
    confirm = client.post('/api/v1/auth/mfa/confirm', json={'code': code})
    assert confirm.status_code == 200
    assert confirm.get_json()['user']['mfa_enabled'] is True
    # Now fully logged in.
    assert client.get('/api/v1/auth/me').status_code == 200


def test_setup_requires_a_pending_login(client, app):
    # No password step first → no pending session.
    assert client.post('/api/v1/auth/mfa/setup').status_code == 401


# --- per-login verification for an enrolled user ---------------------------

def test_enrolled_login_requires_code(client, users):
    resp = client.post('/api/v1/auth/login',
                       json={'username': 'admin', 'password': 'password123'})
    assert resp.status_code == 200
    assert resp.get_json().get('mfa_required') is True
    assert client.get('/api/v1/auth/me').status_code == 401  # not logged in yet


def test_verify_with_valid_code_logs_in(client, users):
    client.post('/api/v1/auth/login',
                json={'username': 'admin', 'password': 'password123'})
    code = pyotp.TOTP(TEST_TOTP_SECRET).now()
    resp = client.post('/api/v1/auth/login/verify', json={'code': code})
    assert resp.status_code == 200
    assert resp.get_json()['user']['username'] == 'admin'
    assert client.get('/api/v1/auth/me').status_code == 200


def test_verify_with_wrong_code_is_rejected(client, users):
    client.post('/api/v1/auth/login',
                json={'username': 'admin', 'password': 'password123'})
    resp = client.post('/api/v1/auth/login/verify', json={'code': '000000'})
    assert resp.status_code == 401
    assert client.get('/api/v1/auth/me').status_code == 401


def test_verify_without_pending_is_rejected(client, users):
    # Straight to verify with no password step.
    assert client.post('/api/v1/auth/login/verify',
                       json={'code': '123456'}).status_code == 401


def test_wrong_password_never_reaches_mfa(client, users):
    resp = client.post('/api/v1/auth/login',
                       json={'username': 'admin', 'password': 'nope'})
    assert resp.status_code == 401
    assert 'mfa_required' not in (resp.get_json() or {})


# --- the conftest login() helper drives the whole flow ---------------------

def test_login_helper_completes_flow(client, users):
    resp = login(client, 'dev')
    assert resp.status_code == 200
    assert resp.get_json()['user']['username'] == 'dev'


# --- admin reset -----------------------------------------------------------

def test_admin_reset_clears_mfa_and_reforces_setup(client, users):
    dev = users['dev']
    login(client, 'admin')
    resp = client.post(f'/api/v1/admin/users/{dev.id}/reset-mfa')
    assert resp.status_code == 200
    assert resp.get_json()['mfa_enabled'] is False

    db.session.expire_all()
    fresh = db.session.get(User, dev.id)
    assert fresh.mfa_enabled is False and fresh.totp_secret is None

    client.post('/api/v1/auth/logout')
    # The developer's next login now forces re-enrollment.
    step = client.post('/api/v1/auth/login',
                       json={'username': 'dev', 'password': 'password123'})
    assert step.get_json().get('mfa_setup_required') is True


def test_reset_mfa_requires_admin(client, users):
    dev = users['dev']
    for username in ('dev', 'ops'):
        login(client, username)
        assert client.post(f'/api/v1/admin/users/{dev.id}/reset-mfa').status_code == 403
        client.post('/api/v1/auth/logout')
