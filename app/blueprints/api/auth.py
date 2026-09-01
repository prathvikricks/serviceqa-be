"""Session-based authentication for the React SPA.

Cookie/session auth via Flask-Login — not bearer tokens. The SPA sends
``credentials: 'include'`` on every call and echoes the CSRF token from
``/auth/csrf`` in an ``X-CSRFToken`` header on anything mutating.
"""
import time
from datetime import datetime

from flask import jsonify, request, session
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf.csrf import generate_csrf

from ...extensions import db, limiter
from ...models.user import User
from ...models.audit import AuditLog
from . import api_bp
from .serializers import user_dict

# A password-verified login is parked here (server session) until the TOTP step
# finishes. No Flask-Login session exists until then. Short-lived on purpose.
_MFA_PENDING_TTL = 300  # seconds


def _set_pending(user, stage):
    session['mfa_pending'] = {'uid': user.id, 'stage': stage, 'ts': time.time()}


def _get_pending(stage=None):
    """Return the pending user if the parked login is valid (and matches
    ``stage`` when given), else None. Expired pending state is cleared."""
    pending = session.get('mfa_pending')
    if not pending:
        return None
    if time.time() - pending.get('ts', 0) > _MFA_PENDING_TTL:
        session.pop('mfa_pending', None)
        return None
    if stage is not None and pending.get('stage') != stage:
        return None
    user = db.session.get(User, pending.get('uid'))
    if user is None or not user.is_active:
        session.pop('mfa_pending', None)
        return None
    return user


def _finish_login(user, remember=False):
    session.pop('mfa_pending', None)
    login_user(user, remember=remember)
    AuditLog.log('user_login', 'user', user.id,
                 user_id=user.id, ip_address=request.remote_addr)
    return jsonify({'user': user_dict(user)})


@api_bp.route('/health')
def health():
    """Liveness probe.

    Reports scheduler state, not just "the process is up". Everything this app
    does happens on a background job, and a dead scheduler is invisible from the
    outside: requests still get approved, they just silently never start.
    """
    from ...services.scheduler_service import scheduler

    running = scheduler.running
    return jsonify({
        'status': 'ok' if running else 'degraded',
        'scheduler': {
            'running': running,
            'timezone': str(scheduler.timezone),
            # Wall-clock the server will compare request windows against — if
            # this doesn't match the team's clock, windows fire at the wrong time.
            'now': datetime.now().isoformat(),
            'jobs': len(scheduler.get_jobs()) if running else 0,
        },
    }), (200 if running else 503)


@api_bp.route('/auth/csrf')
def auth_csrf():
    """Hand the SPA a CSRF token to echo back in the X-CSRFToken header.

    Also establishes the session cookie so subsequent mutating calls validate.
    """
    return jsonify({'csrf_token': generate_csrf()})


@api_bp.route('/auth/login', methods=['POST'])
@limiter.limit('10/minute;50/hour')
def auth_login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    remember = bool(data.get('remember_me', False))

    if not username or not password:
        return jsonify({'error': 'Username and password are required.'}), 400

    # Accept either the username or the email address.
    user = (User.query.filter_by(username=username).first()
            or User.query.filter(db.func.lower(User.email) == username.lower()).first())

    # Same message for "no such user", "wrong password" and "deactivated" so the
    # endpoint can't be used to enumerate accounts.
    if not user or not user.check_password(password) or not user.is_active:
        return jsonify({'error': 'Invalid username or password.'}), 401

    # MFA is mandatory: the password check parks the login, and the second step
    # (verify an existing code, or enroll one first) actually logs the user in.
    if user.mfa_enabled:
        _set_pending(user, 'verify')
        return jsonify({'mfa_required': True})
    _set_pending(user, 'setup')
    return jsonify({'mfa_setup_required': True})


@api_bp.route('/auth/login/verify', methods=['POST'])
@limiter.limit('10/minute;50/hour')
def auth_login_verify():
    """Second login step for an enrolled user: check the authenticator code."""
    user = _get_pending('verify')
    if user is None:
        return jsonify({'error': 'Your login session expired. Start again.'}), 401
    code = (request.get_json(silent=True) or {}).get('code') or ''
    if not user.verify_totp(code):
        return jsonify({'error': 'That code is not valid. Try again.'}), 401
    return _finish_login(user)


@api_bp.route('/auth/mfa/setup', methods=['POST'])
@limiter.limit('10/minute;50/hour')
def auth_mfa_setup():
    """First-login enrollment: mint a secret and return a QR to scan.

    Reachable only with a password-verified pending session — an un-enrolled
    user is not logged in yet."""
    import pyotp
    import qrcode
    import qrcode.image.svg

    user = _get_pending('setup')
    if user is None:
        return jsonify({'error': 'Your login session expired. Start again.'}), 401

    secret = pyotp.random_base32()
    user.set_totp_secret(secret)          # stored, but mfa_enabled stays False
    db.session.commit()

    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name='DevOps Portal')
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    import io
    buf = io.BytesIO()
    img.save(buf)
    qr_svg = buf.getvalue().decode('utf-8')
    return jsonify({'secret': secret, 'otpauth_uri': uri, 'qr_svg': qr_svg})


@api_bp.route('/auth/mfa/confirm', methods=['POST'])
@limiter.limit('10/minute;50/hour')
def auth_mfa_confirm():
    """Finish enrollment: the user proves they scanned the QR, then we log in."""
    user = _get_pending('setup')
    if user is None:
        return jsonify({'error': 'Your login session expired. Start again.'}), 401
    code = (request.get_json(silent=True) or {}).get('code') or ''
    if not user.verify_totp(code):
        return jsonify({'error': 'That code is not valid. Try again.'}), 401
    user.mfa_enabled = True
    db.session.commit()
    AuditLog.log('mfa_enrolled', 'user', user.id,
                 user_id=user.id, ip_address=request.remote_addr)
    return _finish_login(user)


@api_bp.route('/auth/logout', methods=['POST'])
@login_required
def auth_logout():
    logout_user()
    return jsonify({'ok': True})


@api_bp.route('/auth/me')
@login_required
def auth_me():
    """Bootstraps the SPA session on load."""
    return jsonify({'user': user_dict(current_user)})


@api_bp.route('/auth/password', methods=['POST'])
@login_required
def auth_change_password():
    data = request.get_json(silent=True) or {}
    current = data.get('current_password') or ''
    new = data.get('new_password') or ''

    if not current_user.check_password(current):
        return jsonify({'error': 'Current password is incorrect.'}), 400
    if len(new) < 8:
        return jsonify({'error': 'New password must be at least 8 characters.'}), 400

    current_user.set_password(new)
    db.session.commit()
    AuditLog.log('password_changed', 'user', current_user.id,
                 user_id=current_user.id, ip_address=request.remote_addr)
    return jsonify({'ok': True})
