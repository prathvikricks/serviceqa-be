"""
Scheduler service for managing environment start/stop jobs.
Uses APScheduler with SQLAlchemy job store for persistence.
"""
import logging
from datetime import datetime, timezone, timedelta, time as _time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    jobstores={'default': MemoryJobStore()},
    executors={'default': ThreadPoolExecutor(max_workers=5)},
    job_defaults={
        'coalesce': True,
        'max_instances': 1,
        'misfire_grace_time': 300,
    },
)


def init_scheduler(app):
    """Initialize the scheduler with the Flask app."""
    if not scheduler.running:
        # Pin the scheduler to the configured business timezone BEFORE starting
        # — configure() is rejected on a running scheduler. Request windows are
        # stored naive, so this must match the zone those datetimes were written
        # in or every job fires at the wrong wall-clock time.
        tz = app.config.get('SCHEDULER_TIMEZONE')
        if tz:
            scheduler.configure(timezone=tz)
        scheduler.start()
        logger.info(f"APScheduler started (timezone={scheduler.timezone})")

        # Register recurring jobs
        scheduler.add_job(
            orphan_cleanup,
            'interval',
            minutes=15,
            id='orphan_cleanup',
            replace_existing=True,
            args=[app],
        )
        scheduler.add_job(
            sync_environment_status,
            'interval',
            minutes=5,
            id='sync_status',
            replace_existing=True,
            args=[app],
        )


# Python weekday() convention (Mon=0 … Sun=6), matching APScheduler day_of_week tokens.
WEEKDAY_NUM = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}


def _hm(value):
    """Parse 'HH:MM' -> (hour, minute), or None."""
    try:
        hh, mm = value.split(':')
        return int(hh), int(mm)
    except (AttributeError, ValueError):
        return None


def next_occurrence(days, hm, after=None):
    """Next naive-local datetime on any weekday in `days` at time `hm` ('HH:MM'),
    at or after `after` (default now). None if inputs are empty/invalid."""
    after = after or datetime.now()
    parsed = _hm(hm)
    nums = {WEEKDAY_NUM[d] for d in (days or []) if d in WEEKDAY_NUM}
    if parsed is None or not nums:
        return None
    hh, mm = parsed
    for i in range(0, 8):  # today .. same weekday next week (inclusive)
        d = (after + timedelta(days=i)).date()
        if d.weekday() in nums:
            cand = datetime(d.year, d.month, d.day, hh, mm)
            if cand >= after:
                return cand
    return None


def _weekly_window(env_request, after=None):
    """The next (start_dt, stop_dt) window for a weekly request (same-day window)."""
    days = env_request.recurrence_days_list
    start_dt = next_occurrence(days, env_request.start_hm, after=after)
    stop_parsed = _hm(env_request.stop_hm)
    if start_dt is None or stop_parsed is None:
        return None, None
    sh, sm = stop_parsed
    stop_dt = datetime(start_dt.year, start_dt.month, start_dt.day, sh, sm)
    return start_dt, stop_dt


def _complete_cycle(env_request):
    """Called when a request's SECOND action finishes. Records the cycle's cost.
    Weekly requests re-arm (status -> 'approved', window advanced to next occurrence);
    one-time requests terminate (status -> 'completed')."""
    from ..models.budget import CostRecord
    try:
        CostRecord.record_cost(env_request)
    except Exception as e:
        logger.error(f"Cost recording failed: {e}")
    if env_request.is_recurring:
        start_dt, stop_dt = _weekly_window(env_request, after=datetime.now() + timedelta(minutes=1))
        if start_dt and stop_dt:
            env_request.start_time = start_dt
            env_request.end_time = stop_dt
        env_request.status = 'approved'  # armed for the next occurrence (cron stays live)
    else:
        env_request.status = 'completed'


def _update_job_row(request_id, job_type, ok, env_request):
    """Stamp a ScheduledJob after its action ran. One-time jobs terminate
    (completed/failed); weekly jobs stay 'pending' with scheduled_time advanced to
    the next occurrence (the cron trigger remains armed)."""
    from ..models.request import ScheduledJob
    sj = ScheduledJob.query.filter_by(request_id=request_id, job_type=job_type).first()
    if not sj:
        return
    sj.executed_at = datetime.now(timezone.utc)
    if not ok:
        sj.status = 'failed'
    elif env_request.is_recurring:
        sj.status = 'pending'  # cron stays armed for the next occurrence
        first_type = 'stop' if env_request.is_inverse else 'start'
        hm = env_request.start_hm if job_type == first_type else env_request.stop_hm
        nxt = next_occurrence(env_request.recurrence_days_list, hm,
                              after=datetime.now() + timedelta(minutes=1))
        if nxt:
            sj.scheduled_time = nxt
    else:
        sj.status = 'completed'


def _schedule_weekly(app, env_request, first_fn, second_fn, first_type, second_type,
                     first_job_id, second_job_id, now):
    """Arm two recurring cron jobs (first action at start_hm, second at stop_hm)."""
    from ..models.request import ScheduledJob
    from ..extensions import db

    days = env_request.recurrence_days_list
    dow = ",".join(days)
    end_date = (datetime.combine(env_request.recur_until, _time.max)
                if env_request.recur_until else None)

    for jt, jid, hm in ((first_type, first_job_id, env_request.start_hm),
                        (second_type, second_job_id, env_request.stop_hm)):
        sched = next_occurrence(days, hm, after=now) or now
        row = ScheduledJob.query.filter_by(request_id=env_request.id, job_type=jt).first()
        if row:
            row.scheduled_time = sched
            row.apscheduler_job_id = jid
            row.status = 'pending'
            row.executed_at = None
        else:
            db.session.add(ScheduledJob(request_id=env_request.id, job_type=jt,
                                        scheduled_time=sched, apscheduler_job_id=jid))
    db.session.commit()

    for fn, jid, hm in ((first_fn, first_job_id, env_request.start_hm),
                        (second_fn, second_job_id, env_request.stop_hm)):
        parsed = _hm(hm)
        if parsed is None:
            continue
        hh, mm = parsed
        scheduler.add_job(fn, 'cron', day_of_week=dow, hour=hh, minute=mm,
                          id=jid, replace_existing=True, args=[app, env_request.id],
                          end_date=end_date)
    logger.info(f"Scheduled WEEKLY start/stop for request #{env_request.id} "
                f"({dow} {env_request.start_hm}-{env_request.stop_hm})")


def schedule_request(app, env_request):
    """Schedule jobs for an approved request based on action_type + schedule_type."""
    from ..models.request import ScheduledJob
    from ..extensions import db

    with app.app_context():
        now = datetime.now()
        is_inverse = env_request.action_type == 'stop_start'

        # Determine which function runs at start_time and end_time
        # start_stop (default): start at start_time, stop at end_time
        # stop_start (inverse): stop at start_time, start at end_time
        first_fn = _stop_environment if is_inverse else _start_environment
        second_fn = _start_environment if is_inverse else _stop_environment
        first_type = 'stop' if is_inverse else 'start'
        second_type = 'start' if is_inverse else 'stop'

        # Create DB rows FIRST so _start/_stop can update them
        first_job_id = f"{first_type}_{env_request.id}"
        second_job_id = f"{second_type}_{env_request.id}"

        # Recurring weekly schedule uses cron triggers instead of one-off dates.
        if env_request.schedule_type == 'weekly':
            _schedule_weekly(app, env_request, first_fn, second_fn,
                             first_type, second_type, first_job_id, second_job_id, now)
            return

        if not ScheduledJob.query.filter_by(request_id=env_request.id, job_type=first_type).first():
            db.session.add(ScheduledJob(
                request_id=env_request.id,
                job_type=first_type,
                scheduled_time=env_request.start_time,
                apscheduler_job_id=first_job_id,
            ))
        if not ScheduledJob.query.filter_by(request_id=env_request.id, job_type=second_type).first():
            db.session.add(ScheduledJob(
                request_id=env_request.id,
                job_type=second_type,
                scheduled_time=env_request.end_time,
                apscheduler_job_id=second_job_id,
            ))
        db.session.commit()

        # Schedule first action (at start_time)
        if env_request.start_time <= now + timedelta(seconds=60):
            # Run immediately
            first_fn(app, env_request.id)
        else:
            scheduler.add_job(
                first_fn, 'date',
                run_date=env_request.start_time,
                id=first_job_id,
                replace_existing=True,
                args=[app, env_request.id],
            )

        # Schedule second action (at end_time)
        scheduler.add_job(
            second_fn, 'date',
            run_date=env_request.end_time,
            id=second_job_id,
            replace_existing=True,
            args=[app, env_request.id],
        )

    logger.info(f"Scheduled start/stop for request #{env_request.id}")


def cancel_request_jobs(request_id):
    """Cancel all scheduled jobs for a request."""
    for job_type in ['start', 'stop']:
        job_id = f"{job_type}_{request_id}"
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    logger.info(f"Cancelled jobs for request #{request_id}")


def reschedule_stop(request_id, new_end_time):
    """Reschedule the stop job for a request (extension)."""
    job_id = f"stop_{request_id}"
    try:
        end_time = new_end_time.replace(tzinfo=timezone.utc) if new_end_time.tzinfo is None else new_end_time
        scheduler.reschedule_job(job_id, trigger='date', run_date=end_time)
        logger.info(f"Rescheduled stop for request #{request_id} to {new_end_time}")
    except Exception as e:
        logger.error(f"Failed to reschedule stop for request #{request_id}: {e}")


def _start_environment(app, request_id):
    """Start all services for a request."""
    with app.app_context():
        from ..extensions import db
        from ..models.request import EnvironmentRequest, ScheduledJob
        from ..models.audit import AuditLog
        from .cloud_manager import CloudManagerFactory

        env_request = db.session.get(EnvironmentRequest, request_id)
        if not env_request:
            return
        # Allow: approved (normal first action), active (inverse second action)
        if env_request.status not in ('approved', 'pending', 'active'):
            return

        env_request.status = 'starting'
        db.session.commit()

        AuditLog.log('env_starting', 'request', request_id,
                      details={'environment': env_request.environment.display_name})

        project = env_request.environment.project
        try:
            manager = CloudManagerFactory.get_manager(project)
        except Exception as e:
            logger.error(f"Failed to get cloud manager: {e}")
            env_request.status = 'failed'
            db.session.commit()
            AuditLog.log('env_start_failed', 'request', request_id,
                          details={'error': str(e)})
            return

        all_started = True
        for rs in env_request.services.all():
            rs.action_status = 'starting'
            db.session.commit()
            try:
                success = manager.start_service(rs.cloud_service)
                if success:
                    rs.action_status = 'started'
                    rs.started_at = datetime.now(timezone.utc)
                    rs.cloud_service.current_status = 'running'
                else:
                    rs.action_status = 'failed'
                    rs.error_message = 'Start returned False'
                    all_started = False
            except Exception as e:
                rs.action_status = 'failed'
                rs.error_message = str(e)[:500]
                all_started = False
                logger.error(f"Failed to start service {rs.cloud_service.name}: {e}")
            db.session.commit()

        # For normal (start_stop): after start, mark as 'active' (waiting for stop).
        # For inverse (stop_start): start is the SECOND action -> finish the cycle
        # (one-time: 'completed'; weekly: re-armed to 'approved' + cost recorded).
        is_inverse = env_request.action_type == 'stop_start'
        if all_started:
            if is_inverse:
                _complete_cycle(env_request)
            else:
                env_request.status = 'active'
            AuditLog.log('env_started', 'request', request_id)
        else:
            env_request.status = 'failed'
            AuditLog.log('env_start_failed', 'request', request_id,
                          details={'message': 'Some services failed to start'})

        # Update scheduled_jobs table (weekly rows stay armed for the next cycle).
        _update_job_row(request_id, 'start', all_started, env_request)

        db.session.commit()



def _stop_environment(app, request_id):
    """Stop all services for a request."""
    with app.app_context():
        from ..extensions import db
        from ..models.request import EnvironmentRequest, ScheduledJob
        from ..models.audit import AuditLog
        from .cloud_manager import CloudManagerFactory

        env_request = db.session.get(EnvironmentRequest, request_id)
        if not env_request or env_request.status not in ('active', 'starting', 'approved'):
            return

        env_request.status = 'stopping'
        db.session.commit()

        AuditLog.log('env_stopping', 'request', request_id,
                      details={'environment': env_request.environment.display_name})

        project = env_request.environment.project
        try:
            manager = CloudManagerFactory.get_manager(project)
        except Exception as e:
            logger.error(f"Failed to get cloud manager: {e}")
            env_request.status = 'failed'
            db.session.commit()
            return

        all_stopped = True
        for rs in env_request.services.all():
            rs.action_status = 'stopping'
            db.session.commit()
            try:
                success = manager.stop_service(rs.cloud_service)
                if success:
                    rs.action_status = 'stopped'
                    rs.stopped_at = datetime.now(timezone.utc)
                    rs.cloud_service.current_status = 'stopped'
                else:
                    rs.action_status = 'failed'
                    rs.error_message = 'Stop returned False'
                    all_stopped = False
            except Exception as e:
                rs.action_status = 'failed'
                rs.error_message = str(e)[:500]
                all_stopped = False
                logger.error(f"Failed to stop service {rs.cloud_service.name}: {e}")
            db.session.commit()

        # For inverse (stop_start): stop is the FIRST action -> mark 'active' (waiting
        # for the start). For normal (start_stop): stop is the SECOND action -> finish
        # the cycle (one-time: 'completed'; weekly: re-armed to 'approved' + cost).
        is_inverse = env_request.action_type == 'stop_start'
        if all_stopped:
            if is_inverse:
                env_request.status = 'active'  # first action; waiting to start again
            else:
                _complete_cycle(env_request)
        else:
            env_request.status = 'failed'

        _update_job_row(request_id, 'stop', all_stopped, env_request)

        db.session.commit()

        AuditLog.log('env_stopped' if all_stopped else 'env_stop_failed',
                      'request', request_id)



def orphan_cleanup(app):
    """Safety net: stop environments whose end_time has passed but are still active."""
    with app.app_context():
        from ..models.request import EnvironmentRequest

        now = datetime.now(timezone.utc)
        overdue = EnvironmentRequest.query.filter(
            EnvironmentRequest.status.in_(['active', 'starting']),
            EnvironmentRequest.end_time < now,
            # Weekly requests have a rolling window (end_time = next occurrence);
            # their own stop cron handles teardown — never orphan-stop them.
            EnvironmentRequest.schedule_type != 'weekly',
        ).all()

        for req in overdue:
            logger.warning(f"Orphan cleanup: stopping overdue request #{req.id}")
            _stop_environment(app, req.id)


def sync_environment_status(app):
    """Poll cloud providers for actual service status."""
    with app.app_context():
        from ..extensions import db
        from ..models.environment import CloudService
        from ..models.project import Project
        from .cloud_manager import CloudManagerFactory

        services = CloudService.query.filter_by(is_active=True).all()
        for service in services:
            try:
                project = service.environment.project
                manager = CloudManagerFactory.get_manager(project)
                status = manager.get_status(service)
                service.current_status = status
                service.last_status_check = datetime.now(timezone.utc)
            except Exception as e:
                logger.debug(f"Status sync failed for {service.name}: {e}")
                service.current_status = 'unknown'

        db.session.commit()
