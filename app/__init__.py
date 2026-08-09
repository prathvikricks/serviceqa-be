import os

from flask import Flask, jsonify, request
from flask_cors import CORS

from .config import config
from .extensions import db, login_manager, csrf, migrate, limiter


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')

    app = Flask(__name__)
    app.config.from_object(config.get(config_name, config['default']))

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)

    # Allow the React SPA to call the JSON API with session cookies.
    CORS(
        app,
        resources={r"/api/*": {"origins": app.config['FRONTEND_ORIGIN']}},
        supports_credentials=True,
    )

    from .blueprints.api import api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')

    from .models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # API-only app — unauthenticated requests get a JSON 401 and the SPA handles
    # the redirect to /login itself.
    @login_manager.unauthorized_handler
    def _handle_unauthorized():
        return jsonify({'error': 'Authentication required.'}), 401

    @app.errorhandler(403)
    def _handle_forbidden(err):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'You do not have permission to do that.'}), 403
        return err

    @app.errorhandler(404)
    def _handle_not_found(err):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not found.'}), 404
        return err

    @app.errorhandler(429)
    def _handle_rate_limited(err):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Too many attempts. Please try again later.'}), 429
        return err

    # Import every model so Alembic autogenerate sees the full metadata.
    with app.app_context():
        from . import models  # noqa: F401

    return app


def start_background_jobs(app):
    """Start APScheduler and re-arm jobs for already-approved requests.

    Deliberately NOT called from create_app(). Whether a given process should
    own the scheduler depends on how it's being served — under the Werkzeug
    reloader only the child should arm jobs, or every job runs twice — and only
    the entrypoint knows that. Inferring it inside the factory meant any process
    that guessed wrong started no scheduler at all, and the failure was silent:
    requests got approved and then simply never started.

    So: run.py and wsgi.py each call this explicitly. Tests never do.
    """
    from .services.scheduler_service import init_scheduler

    init_scheduler(app)
    try:
        recover_pending_jobs(app)
    except Exception:
        app.logger.warning('Job recovery skipped (tables may not exist yet)')


def recover_pending_jobs(app):
    """Re-arm scheduler jobs for approved/active requests after a restart.

    APScheduler uses an in-memory job store, so every job is lost on restart.
    The DB rows are the source of truth — this rebuilds the in-memory schedule
    from them. Without it, an approved request silently never starts.
    """
    from datetime import datetime
    from sqlalchemy import or_, and_

    with app.app_context():
        from .models.request import EnvironmentRequest
        from .services.scheduler_service import schedule_request, scheduler

        now = datetime.now()  # naive local — matches how request times are stored
        today = now.date()
        # Re-arm for: one-time requests whose window hasn't fully passed, and
        # weekly requests still inside their recurrence bound (perpetual if null).
        to_recover = EnvironmentRequest.query.filter(
            EnvironmentRequest.status.in_(['approved', 'active']),
            or_(
                and_(EnvironmentRequest.schedule_type != 'weekly',
                     EnvironmentRequest.end_time > now),
                and_(EnvironmentRequest.schedule_type == 'weekly',
                     or_(EnvironmentRequest.recur_until.is_(None),
                         EnvironmentRequest.recur_until >= today)),
            ),
        ).all()

        existing_ids = {j.id for j in scheduler.get_jobs()}
        for req in to_recover:
            if {f'start_{req.id}', f'stop_{req.id}'} <= existing_ids:
                continue
            try:
                schedule_request(app, req)
                app.logger.info(f'Recovered jobs for request #{req.id}')
            except Exception as e:
                app.logger.error(f'Failed to recover jobs for request #{req.id}: {e}')
