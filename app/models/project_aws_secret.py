from datetime import datetime, timezone

from ..extensions import db


class ProjectAwsSecret(db.Model):
    """A reference linking a project to a secret in AWS Secrets Manager.

    Unlike ProjectSecret, this stores NO value — only a reference (ARN, name,
    region). The value is fetched live from AWS on reveal, so AWS remains the
    single source of truth. NULL ``environment_id`` = the whole project.
    """
    __tablename__ = 'project_aws_secrets'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False,
                           index=True)
    environment_id = db.Column(db.Integer, db.ForeignKey('environments.id'), nullable=True)
    # The AWS reference. arn is what we read; name/region are for display and to
    # target the right regional client (Secrets Manager is regional).
    aws_arn = db.Column(db.String(2048), nullable=False)
    aws_name = db.Column(db.String(512), nullable=False)
    aws_region = db.Column(db.String(32), nullable=False)
    # The key developers see. Defaults to the last path segment of aws_name.
    display_key = db.Column(db.String(100), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project = db.relationship('Project')
    environment = db.relationship('Environment')
    creator = db.relationship('User', foreign_keys=[created_by])

    __table_args__ = (
        # NULL environment_id (project-wide) isn't caught by SQL uniqueness, so
        # the associate endpoint also checks explicitly.
        db.UniqueConstraint('project_id', 'environment_id', 'aws_arn',
                            name='uq_project_aws_secret'),
    )

    @property
    def scope_label(self):
        return self.environment.display_name if self.environment else 'All environments'

    def __repr__(self):
        return f'<ProjectAwsSecret {self.display_key} (project={self.project_id})>'
