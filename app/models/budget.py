from datetime import datetime, timezone
from ..extensions import db


class CostRecord(db.Model):
    """Tracks actual runtime cost per request."""
    __tablename__ = 'cost_records'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('environment_requests.id'), nullable=False)
    environment_id = db.Column(db.Integer, db.ForeignKey('environments.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    runtime_hours = db.Column(db.Float, default=0.0)
    cost = db.Column(db.Float, default=0.0)
    month = db.Column(db.String(7), nullable=False)  # '2026-04'
    recorded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    request = db.relationship('EnvironmentRequest', backref='cost_record')
    environment = db.relationship('Environment')
    project = db.relationship('Project')

    def __repr__(self):
        return f'<CostRecord req={self.request_id} ${self.cost}>'

    @staticmethod
    def record_cost(env_request):
        """Calculate and record cost when a request completes."""
        from ..models.request import RequestService
        total_hours = 0.0
        total_cost = 0.0

        for rs in env_request.services.all():
            if rs.started_at and rs.stopped_at:
                hours = (rs.stopped_at - rs.started_at).total_seconds() / 3600
            elif rs.started_at:
                hours = (datetime.now(timezone.utc) - rs.started_at).total_seconds() / 3600
            else:
                hours = 0.0

            svc_cost = hours * (rs.cloud_service.hourly_cost or 0)
            total_hours += hours
            total_cost += svc_cost

        month = env_request.start_time.strftime('%Y-%m')

        record = CostRecord(
            request_id=env_request.id,
            environment_id=env_request.environment_id,
            project_id=env_request.environment.project_id,
            runtime_hours=round(total_hours, 2),
            cost=round(total_cost, 2),
            month=month,
        )
        db.session.add(record)
        db.session.commit()
        return record
