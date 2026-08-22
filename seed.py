"""Idempotent bootstrap: create tables, seed roles, and ensure an admin exists.

Runs on every container start (see entrypoint.sh) *after* `flask db upgrade`.
`create_all()` is a belt-and-braces backstop for a fresh volume — Alembic owns
schema changes, so a new column still needs a migration.

With --demo it also plants a demo project so the whole request → approve →
schedule flow is clickable immediately, in mock mode, with no cloud account.
"""
import sys

from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models.user import Role, User
from app.models.project import Project
from app.models.environment import Environment, CloudService


# New columns added to environment_requests after the table already shipped.
# create_all() only creates missing *tables*, never missing columns, and this
# app has no Alembic history — so we patch the live schema idempotently. Postgres
# only; on SQLite a fresh create_all() already has the current schema.
_REQUEST_COLUMN_DDL = [
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS "
    "request_type VARCHAR(20) NOT NULL DEFAULT 'service'",
    "ALTER TABLE environment_requests ALTER COLUMN environment_id DROP NOT NULL",
    "ALTER TABLE environment_requests ALTER COLUMN start_time DROP NOT NULL",
    "ALTER TABLE environment_requests ALTER COLUMN end_time DROP NOT NULL",
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS "
    "project_id INTEGER REFERENCES projects(id)",
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS repo_name VARCHAR(120)",
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS repo_description TEXT",
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS repo_visibility VARCHAR(10)",
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS git_provider VARCHAR(20)",
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS repo_url VARCHAR(500)",
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS git_error TEXT",
    # Must stay last: chat_conversations is created by create_all(), which
    # main() runs before this patch.
    "ALTER TABLE environment_requests ADD COLUMN IF NOT EXISTS "
    "conversation_id INTEGER REFERENCES chat_conversations(id)",
]


def ensure_request_columns():
    """Idempotently add the repo-request columns to an existing table (Postgres)."""
    if db.engine.dialect.name != 'postgresql':
        print('  schema: non-postgres, create_all() owns the schema — skipping patch')
        return
    with db.engine.begin() as conn:
        for stmt in _REQUEST_COLUMN_DDL:
            conn.execute(text(stmt))
    print('  schema: environment_requests repo columns ensured')


# Added to project_members after the table already shipped. Same reasoning as
# _REQUEST_COLUMN_DDL above: no Alembic history, so patch the live schema
# idempotently. Postgres only.
_MEMBER_COLUMN_DDL = [
    "ALTER TABLE project_members ADD COLUMN IF NOT EXISTS "
    "project_role VARCHAR(20) NOT NULL DEFAULT 'developer'",
]


def ensure_member_columns():
    """Idempotently add the project_role column to an existing table (Postgres)."""
    if db.engine.dialect.name != 'postgresql':
        print('  schema: non-postgres, create_all() owns the schema — skipping patch')
        return
    with db.engine.begin() as conn:
        for stmt in _MEMBER_COLUMN_DDL:
            conn.execute(text(stmt))
    print('  schema: project_members project_role ensured')


def backfill_project_devops():
    """Give every existing global-devops user project-devops on every project.

    Before this change any devops could approve anything. Without a backfill
    the upgrade would empty every approval inbox on deploy and strand pending
    requests. Idempotent: it only adds what is missing, and never demotes.
    """
    from app.models.user import ProjectMember

    devops_users = [u for u in User.query.all() if u.role.name == 'devops']
    if not devops_users:
        print('  members: no global devops users to backfill')
        return

    added = promoted = 0
    for project in Project.query.filter_by(is_active=True).all():
        for user in devops_users:
            member = ProjectMember.query.filter_by(
                project_id=project.id, user_id=user.id).first()
            if member is None:
                db.session.add(ProjectMember(
                    project_id=project.id, user_id=user.id,
                    added_by=user.id, project_role='devops'))
                added += 1
            elif member.project_role != 'devops':
                member.project_role = 'devops'
                promoted += 1
    db.session.commit()
    print(f'  members: project-devops backfilled ({added} added, {promoted} promoted)')


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
        ensure_request_columns()
        ensure_member_columns()
        seed_roles()
        seed_admin(app)
        if '--demo' in sys.argv:
            seed_demo()
        # After the demo projects exist, so they are covered too.
        backfill_project_devops()
        print('==> Done')


if __name__ == '__main__':
    main()
