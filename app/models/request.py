from datetime import datetime, timezone
from ..extensions import db


class EnvironmentRequest(db.Model):
    __tablename__ = 'environment_requests'

    STATUSES = [
        'pending', 'approved', 'declined', 'starting', 'active',
        'stopping', 'completed', 'failed', 'cancelled', 'extension_pending'
    ]

    ACTION_TYPES = [
        ('start_stop', 'Start → Stop'),   # Start services at start_time, stop at end_time
        ('stop_start', 'Stop → Start'),    # Stop services at start_time, start at end_time
    ]

    # 'service' = the classic environment start/stop request (needs an
    # environment + time window). 'repo' = a Git repository creation request,
    # fulfilled by the approver choosing a provider (github/gitlab) at approval
    # time — no environment, no schedule.
    REQUEST_TYPES = ['service', 'repo']

    id = db.Column(db.Integer, primary_key=True)
    requester_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    # Discriminator. Repo requests leave environment_id/start_time/end_time null.
    request_type = db.Column(db.String(20), default='service', nullable=False)
    environment_id = db.Column(db.Integer, db.ForeignKey('environments.id'), nullable=True)
    action_type = db.Column(db.String(20), default='start_stop', nullable=False)
    # For a one-time request start_time/end_time is the exact window. For a
    # weekly request they hold the NEXT upcoming occurrence (refreshed each cycle)
    # so duration/cost/list displays keep working; the recurrence is driven by
    # recurrence_days + start_hm/stop_hm below. Null for repo requests.
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    reason = db.Column(db.Text, nullable=False)

    # --- Repo-creation request fields (request_type == 'repo') ---
    # The project/team this repo belongs to (independent of environments).
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    repo_name = db.Column(db.String(120), nullable=True)
    repo_description = db.Column(db.Text, nullable=True)
    repo_visibility = db.Column(db.String(10), nullable=True)   # 'private' | 'public'
    git_provider = db.Column(db.String(20), nullable=True)      # chosen by approver: 'github' | 'gitlab'
    repo_url = db.Column(db.String(500), nullable=True)         # populated once created
    git_error = db.Column(db.Text, nullable=True)              # last creation failure, if any
    # Recurrence: 'once' (default, one-shot) or 'weekly' (repeat on chosen
    # weekdays at a daily time window until recur_until, or forever if null).
    schedule_type = db.Column(db.String(20), default='once', nullable=False)
    recurrence_days = db.Column(db.String(30), nullable=True)   # APScheduler tokens, e.g. 'mon,wed,fri'
    start_hm = db.Column(db.String(5), nullable=True)           # first action time-of-day, 'HH:MM'
    stop_hm = db.Column(db.String(5), nullable=True)            # second action time-of-day, 'HH:MM'
    recur_until = db.Column(db.Date, nullable=True)             # optional last day (inclusive)
    status = db.Column(db.String(20), default='pending', nullable=False)
    estimated_cost = db.Column(db.Float, nullable=True)
    parent_request_id = db.Column(db.Integer, db.ForeignKey('environment_requests.id'),
                                  nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    # Named repo_project (not 'project') so it doesn't shadow the `project`
    # property below, which resolves to the environment's project for service
    # requests and this link for repo requests.
    repo_project = db.relationship('Project', foreign_keys=[project_id])
    services = db.relationship('RequestService', backref='request', lazy='dynamic',
                               cascade='all, delete-orphan')
    approval = db.relationship('Approval', backref='request', uselist=False)
    scheduled_jobs = db.relationship('ScheduledJob', backref='request', lazy='dynamic',
                                     cascade='all, delete-orphan')
    extensions = db.relationship('EnvironmentRequest', backref=db.backref(
        'parent_request', remote_side='EnvironmentRequest.id'), lazy='dynamic')

    # Ordered weekday tokens for label rendering + validation.
    WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    _WEEKDAY_LABEL = {'mon': 'Mon', 'tue': 'Tue', 'wed': 'Wed', 'thu': 'Thu',
                      'fri': 'Fri', 'sat': 'Sat', 'sun': 'Sun'}

    @property
    def is_repo(self):
        return self.request_type == 'repo'

    @property
    def is_recurring(self):
        return self.schedule_type == 'weekly'

    @property
    def recurrence_days_list(self):
        """Weekday tokens in canonical Mon→Sun order (empty for one-time)."""
        raw = {d.strip() for d in (self.recurrence_days or '').split(',') if d.strip()}
        return [d for d in self.WEEKDAYS if d in raw]

    @property
    def recurrence_label(self):
        """Human summary of the schedule, e.g.
        'Weekly · Mon, Wed, Fri · 09:00–17:00 · until 2026-12-31' (None if one-time)."""
        if not self.is_recurring:
            return None
        days = self.recurrence_days_list
        days_txt = ('Every day' if len(days) == 7
                    else ', '.join(self._WEEKDAY_LABEL[d] for d in days) or '—')
        window = f"{self.start_hm}–{self.stop_hm}" if self.start_hm and self.stop_hm else ''
        parts = ['Weekly', days_txt]
        if window:
            parts.append(window)
        if self.recur_until:
            parts.append(f"until {self.recur_until.isoformat()}")
        return ' · '.join(parts)

    @property
    def is_inverse(self):
        return self.action_type == 'stop_start'

    @property
    def action_label(self):
        if self.is_repo:
            return 'Create Repo'
        return 'Stop → Start' if self.is_inverse else 'Start → Stop'

    @property
    def first_action(self):
        return 'stop' if self.is_inverse else 'start'

    @property
    def second_action(self):
        return 'start' if self.is_inverse else 'stop'

    @property
    def duration_hours(self):
        if not self.start_time or not self.end_time:
            return None
        delta = self.end_time - self.start_time
        return round(delta.total_seconds() / 3600, 1)

    @property
    def is_active_now(self):
        now = datetime.now(timezone.utc)
        return self.status == 'active' and self.start_time <= now <= self.end_time

    @property
    def project(self):
        # Service requests inherit their project from the environment; repo
        # requests carry a direct project link instead.
        if self.environment is not None:
            return self.environment.project
        return self.repo_project

    def __repr__(self):
        return f'<EnvironmentRequest #{self.id} ({self.status})>'


class RequestService(db.Model):
    __tablename__ = 'request_services'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('environment_requests.id'), nullable=False)
    cloud_service_id = db.Column(db.Integer, db.ForeignKey('cloud_services.id'), nullable=False)
    action_status = db.Column(db.String(20), default='pending')
    # pending, starting, started, stopping, stopped, failed
    started_at = db.Column(db.DateTime, nullable=True)
    stopped_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f'<RequestService req={self.request_id} svc={self.cloud_service_id} ({self.action_status})>'


class ScheduledJob(db.Model):
    __tablename__ = 'scheduled_jobs'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('environment_requests.id'), nullable=False)
    job_type = db.Column(db.String(20), nullable=False)  # 'start', 'stop', 'health_check'
    scheduled_time = db.Column(db.DateTime, nullable=False)
    executed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, running, completed, failed
    error_message = db.Column(db.Text, nullable=True)
    apscheduler_job_id = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f'<ScheduledJob {self.job_type} for req={self.request_id} ({self.status})>'
