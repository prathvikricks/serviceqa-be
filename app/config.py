import os

basedir = os.path.abspath(os.path.dirname(__file__))


def _db_url():
    """Read DATABASE_URL, normalizing the legacy ``postgres://`` scheme that
    managed providers (Aiven, Render, Heroku) still emit — SQLAlchemy 1.4+ only
    accepts ``postgresql://``. Falls back to a local SQLite file."""
    url = os.environ.get('DATABASE_URL')
    if not url:
        # Create instance/ on demand — SQLite won't make the directory itself,
        # and a fresh clone has no reason to have one.
        instance = os.path.join(os.path.dirname(basedir), 'instance')
        os.makedirs(instance, exist_ok=True)
        return 'sqlite:///' + os.path.join(instance, 'evnmanager.db')
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    return url


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True

    # Fernet key (urlsafe-base64) for encrypting stored cloud credentials. If
    # unset, one is derived from SECRET_KEY (fine for dev). Set it explicitly in
    # production and keep it stable — rotating SECRET_KEY without it would
    # orphan every credential already encrypted.
    CRED_KEY = os.environ.get('CRED_KEY')

    # React SPA origin allowed to call the JSON API with credentials.
    FRONTEND_ORIGIN = os.environ.get('FRONTEND_ORIGIN', 'http://localhost:5173')

    # Session cookie defaults — SPA-friendly. Overridden per-env below.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # Rate limiting (auth endpoints only). In-memory suits a single instance;
    # point at Redis (redis://…) when scaling out.
    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')

    # THE timezone of the whole scheduling system.
    #
    # Request windows are stored as NAIVE datetimes and weekly rules as plain
    # 'HH:MM' — "start UAT at 09:00 on Mondays" means 09:00 for the team, and
    # should keep meaning that across DST. So both the app and APScheduler are
    # pinned to one business timezone rather than each picking up whatever the
    # host happens to be set to.
    #
    # Set this to your team's zone (e.g. Asia/Kolkata, Europe/London). If it
    # disagrees with what users see in the browser, every scheduled window fires
    # at the wrong time — a container defaulting to UTC while the team is on
    # UTC+5:30 arms each job 5½ hours late.
    SCHEDULER_TIMEZONE = os.environ.get('TZ', 'UTC')

    # Seed admin — created on first boot if the users table is empty.
    SEED_ADMIN_USERNAME = os.environ.get('SEED_ADMIN_USERNAME', 'admin')
    SEED_ADMIN_EMAIL = os.environ.get('SEED_ADMIN_EMAIL', 'admin@example.com')
    SEED_ADMIN_PASSWORD = os.environ.get('SEED_ADMIN_PASSWORD', 'admin123')


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = _db_url()


class ProductionConfig(Config):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = _db_url()
    # 'Lax' when the SPA is same-origin; set SESSION_COOKIE_SAMESITE=None for a
    # cross-origin SPA (None also requires Secure=True, below).
    SESSION_COOKIE_SAMESITE = os.environ.get('SESSION_COOKIE_SAMESITE', 'Lax')
    # HTTPS-only cookies by default. Local docker-compose serves plain HTTP, and
    # a browser silently drops a Secure cookie there — which looks like "login
    # does nothing" — so compose sets this to 0. Never do that on a real deploy.
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', '1') == '1'
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_HTTPONLY = True
    # Flask-WTF's strict-referer check compares Referer against THIS API's host,
    # so a cross-origin SPA always fails it. The CSRF double-submit token plus
    # the CORS allow-list are the real protection. Re-enable for same-origin.
    WTF_CSRF_SSL_STRICT = os.environ.get('WTF_CSRF_SSL_STRICT', 'false').lower() == 'true'


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig,
}
