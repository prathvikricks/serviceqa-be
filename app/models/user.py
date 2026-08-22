from datetime import datetime, timezone
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from ..extensions import db


class Role(db.Model):
    __tablename__ = 'roles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

    users = db.relationship('User', backref='role', lazy='dynamic')

    def __repr__(self):
        return f'<Role {self.name}>'

    @staticmethod
    def seed():
        for name in ['developer', 'devops', 'admin']:
            if not Role.query.filter_by(name=name).first():
                db.session.add(Role(name=name))
        db.session.commit()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    project_memberships = db.relationship('ProjectMember', backref='user',
                                          foreign_keys='ProjectMember.user_id', lazy='dynamic')
    requests = db.relationship('EnvironmentRequest', backref='requester', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role.name == 'admin'

    @property
    def is_devops(self):
        return self.role.name in ('devops', 'admin')

    @property
    def is_developer(self):
        return self.role.name == 'developer'

    def get_projects(self):
        """Projects this user can see. devops/admin see every active project;
        a developer sees only the ones they've been added to."""
        from .project import Project
        if self.is_devops:
            return Project.query.filter_by(is_active=True).all()
        return [m.project for m in self.project_memberships.all() if m.project.is_active]

    def is_member_of(self, project_id):
        """True if the user can access this project."""
        from .project import Project
        proj = db.session.get(Project, project_id)
        if proj is None:
            return False
        if self.is_devops:
            return True
        return self.project_memberships.filter_by(project_id=project_id).first() is not None

    def can_view_secrets_of(self, project_id):
        """True if the user may reveal this project's secret values.

        DevOps and admins always can — they administer and operate the thing.
        A developer needs the per-membership `can_view_secrets` flag: being a
        member is enough to raise requests, but not to read credentials.
        """
        from .project import Project
        if db.session.get(Project, project_id) is None:
            return False
        if self.is_devops:
            return True
        membership = self.project_memberships.filter_by(project_id=project_id).first()
        return bool(membership and membership.can_view_secrets)

    def is_project_devops(self, project_id):
        """True if this user may approve requests belonging to this project.

        Admins always may — they are the catch-all approver. Everyone else
        needs an explicit `project_role='devops'` membership: holding the
        global `devops` role grants operational reach (emergency stop,
        cross-project visibility) but no longer implies approval rights on a
        project nobody put you on.
        """
        from .project import Project
        if db.session.get(Project, project_id) is None:
            return False
        if self.is_admin:
            return True
        membership = self.project_memberships.filter_by(project_id=project_id).first()
        return bool(membership and membership.project_role == 'devops')

    def __repr__(self):
        return f'<User {self.username}>'


class ProjectMember(db.Model):
    __tablename__ = 'project_members'

    ROLES = ['developer', 'devops']

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    added_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    # Membership alone lets a developer raise requests against the project.
    # Reading its stored secrets is a separate, explicitly-granted permission,
    # so it defaults off — see User.can_view_secrets_of.
    can_view_secrets = db.Column(db.Boolean, default=False, nullable=False)
    # What this user IS on the project, independent of their global role.
    # 'devops' is what routes a request's approval here — see
    # User.is_project_devops. Defaults to 'developer' so adding a member never
    # silently grants approval rights.
    project_role = db.Column(db.String(20), default='developer', nullable=False)
    added_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    adder = db.relationship('User', foreign_keys=[added_by])

    __table_args__ = (
        db.UniqueConstraint('project_id', 'user_id', name='uq_project_member'),
    )

    def __repr__(self):
        return f'<ProjectMember project={self.project_id} user={self.user_id}>'
