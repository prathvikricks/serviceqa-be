import pytest

from app import create_app
from app.extensions import db as _db
from app.models.user import Role, User, ProjectMember
from app.models.project import Project
from app.models.environment import Environment, CloudService


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        _db.create_all()
        Role.seed()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


# A fixed TOTP seed so tests can compute valid codes. MFA is mandatory, so every
# test user is pre-enrolled and the login() helper completes the code step.
TEST_TOTP_SECRET = 'JBSWY3DPEHPK3PXP'


def make_user(username, role_name, password='password123'):
    role = Role.query.filter_by(name=role_name).first()
    user = User(username=username, email=f'{username}@example.com', role_id=role.id)
    user.set_password(password)
    user.set_totp_secret(TEST_TOTP_SECRET)
    user.mfa_enabled = True
    _db.session.add(user)
    _db.session.commit()
    return user


@pytest.fixture
def users(app):
    return {
        'admin': make_user('admin', 'admin'),
        'devops': make_user('ops', 'devops'),
        'dev': make_user('dev', 'developer'),
    }


@pytest.fixture
def project(app, users):
    """A mock-mode AWS project with one environment holding two services."""
    p = Project(name='Demo', slug='demo', cloud_provider='aws', mode='mock',
                created_by=users['admin'].id)
    p.set_provider_config({'region': 'us-east-1'})
    _db.session.add(p)
    _db.session.flush()

    env = Environment(project_id=p.id, name='uat', display_name='UAT')
    _db.session.add(env)
    _db.session.flush()

    for name, rid, cost in [('Web', 'i-0mock1', 0.10), ('DB', 'mock-pg', 0.20)]:
        _db.session.add(CloudService(
            environment_id=env.id, name=name, service_type='ec2',
            cloud_resource_id=rid, hourly_cost=cost, current_status='stopped'))

    # Developers only reach projects they're a member of; devops/admin see all.
    _db.session.add(ProjectMember(project_id=p.id, user_id=users['dev'].id,
                                  added_by=users['admin'].id))
    _db.session.commit()
    return p


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username, password='password123'):
    """Log in through the two-step MFA flow. Returns the final (verified)
    response so callers can treat it like a completed login."""
    import pyotp
    resp = client.post('/api/v1/auth/login',
                       json={'username': username, 'password': password})
    body = resp.get_json() or {}
    # Bad credentials / inactive — hand back the 401 as-is.
    if not (body.get('mfa_required') or body.get('mfa_setup_required')):
        return resp
    code = pyotp.TOTP(TEST_TOTP_SECRET).now()
    return client.post('/api/v1/auth/login/verify', json={'code': code})
