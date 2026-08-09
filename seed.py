"""Idempotent bootstrap: create tables, seed roles, and ensure an admin exists.

Runs on every container start (see entrypoint.sh) *after* `flask db upgrade`.
`create_all()` is a belt-and-braces backstop for a fresh volume — Alembic owns
schema changes, so a new column still needs a migration.

With --demo it also plants a demo project so the whole request → approve →
schedule flow is clickable immediately, in mock mode, with no cloud account.
"""
import sys

from app import create_app
from app.extensions import db
from app.models.user import Role, User
from app.models.project import Project
from app.models.environment import Environment, CloudService


def seed_roles():
    Role.seed()
    print('  roles: developer / devops / admin')


def seed_admin(app):
    if User.query.count():
        print('  admin: users already exist, skipping')
        return

    username = app.config['SEED_ADMIN_USERNAME']
    password = app.config['SEED_ADMIN_PASSWORD']
    admin_role = Role.query.filter_by(name='admin').first()

    admin = User(username=username, email=app.config['SEED_ADMIN_EMAIL'],
                 role_id=admin_role.id)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    print(f'  admin: created "{username}" — CHANGE THIS PASSWORD')


def seed_demo():
    """A mock project with two environments and a few services."""
    if Project.query.filter_by(slug='demo-platform').first():
        print('  demo: already present, skipping')
        return

    admin = User.query.join(Role).filter(Role.name == 'admin').first()
    project = Project(
        name='Demo Platform',
        slug='demo-platform',
        description='Sample project in mock mode — safe to start and stop.',
        cloud_provider='aws',
        mode='mock',
        created_by=admin.id,
    )
    project.set_provider_config({'region': 'us-east-1'})
    db.session.add(project)
    db.session.flush()

    for name, display, services in [
        ('dev', 'Development', [
            ('Web Server', 'ec2', 'i-0mock1111aaaa2222', 0.0104),
            ('API Server', 'ec2', 'i-0mock3333bbbb4444', 0.0208),
        ]),
        ('uat', 'UAT', [
            ('UAT Web', 'ec2', 'i-0mock5555cccc6666', 0.0416),
            ('UAT Database', 'rds', 'mock-postgres-01', 0.0680),
        ]),
    ]:
        env = Environment(project_id=project.id, name=name, display_name=display,
                          region='us-east-1')
        db.session.add(env)
        db.session.flush()
        for svc_name, svc_type, resource_id, cost in services:
            db.session.add(CloudService(
                environment_id=env.id, name=svc_name, service_type=svc_type,
                cloud_resource_id=resource_id, hourly_cost=cost,
                current_status='stopped', cloud_config={'region': 'us-east-1'}))

    db.session.commit()
    print('  demo: "Demo Platform" with dev + uat environments')


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        print('==> Seeding')
        seed_roles()
        seed_admin(app)
        if '--demo' in sys.argv:
            seed_demo()
        print('==> Done')


if __name__ == '__main__':
    main()
