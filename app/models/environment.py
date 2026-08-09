from datetime import datetime, timezone
from ..extensions import db


class Environment(db.Model):
    __tablename__ = 'environments'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)  # 'dev', 'uat', 'staging'
    display_name = db.Column(db.String(100), nullable=False)  # 'Development', 'UAT'
    resource_group = db.Column(db.String(200), nullable=True)  # Azure resource group
    region = db.Column(db.String(50), nullable=True)  # AWS region override
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    services = db.relationship('CloudService', backref='environment', lazy='dynamic',
                               cascade='all, delete-orphan')
    requests = db.relationship('EnvironmentRequest', backref='environment', lazy='dynamic')

    __table_args__ = (
        db.UniqueConstraint('project_id', 'name', name='uq_project_environment'),
    )

    @property
    def active_services(self):
        return self.services.filter_by(is_active=True).all()

    @property
    def total_hourly_cost(self):
        return sum(s.hourly_cost or 0 for s in self.active_services)

    def __repr__(self):
        return f'<Environment {self.name} (project={self.project_id})>'


class CloudService(db.Model):
    __tablename__ = 'cloud_services'

    id = db.Column(db.Integer, primary_key=True)
    environment_id = db.Column(db.Integer, db.ForeignKey('environments.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)  # 'Web App', 'API VM'
    service_type = db.Column(db.String(30), nullable=False)
    # Azure: vm, app_service, aks, sql_db, container_app
    # AWS: ec2, rds, ecs, lambda_fn
    cloud_resource_id = db.Column(db.String(500), nullable=False)  # ARM ID or AWS ARN
    cloud_config = db.Column(db.JSON, nullable=True)  # Extra params
    hourly_cost = db.Column(db.Float, default=0.0)
    current_status = db.Column(db.String(20), default='unknown')
    # running, stopped, deallocated, unknown
    last_status_check = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    # Relationships
    request_services = db.relationship('RequestService', backref='cloud_service', lazy='dynamic')

    SERVICE_TYPES = {
        'azure': ['vm', 'vmss', 'app_service', 'aks', 'sql_db', 'mysql', 'container_app'],
        'aws': ['ec2', 'rds', 'ecs', 'lambda_fn'],
    }

    SERVICE_TYPE_LABELS = {
        'vm': 'Virtual Machine',
        'vmss': 'VM Scale Set',
        'app_service': 'App Service',
        'aks': 'AKS Cluster',
        'sql_db': 'SQL Database',
        'mysql': 'MySQL Flexible Server',
        'container_app': 'Container App',
        'ec2': 'EC2 Instance',
        'rds': 'RDS Instance',
        'ecs': 'ECS Service',
        'lambda_fn': 'Lambda Function',
    }

    def __repr__(self):
        return f'<CloudService {self.name} ({self.service_type})>'
