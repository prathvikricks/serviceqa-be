"""Session-based authentication for the React SPA.

Cookie/session auth via Flask-Login — not bearer tokens. The SPA sends
``credentials: 'include'`` on every call and echoes the CSRF token from
``/auth/csrf`` in an ``X-CSRFToken`` header on anything mutating.
"""
from datetime import datetime

from flask import jsonify, request
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf.csrf import generate_csrf

from ...extensions import db, limiter
from ...models.user import User
from ...models.audit import AuditLog
from . import api_bp
from .serializers import user_dict


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

    login_user(user, remember=remember)
    AuditLog.log('user_login', 'user', user.id,
                 user_id=user.id, ip_address=request.remote_addr)
    return jsonify({'user': user_dict(user)})


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
