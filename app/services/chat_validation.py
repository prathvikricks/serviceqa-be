"""Turn a model-proposed draft into a trusted one, or into a list of problems.

The chat agent's output is untrusted input. A model can name an environment in
someone else's project, a service that does not exist, or a date in the past —
and the request forms downstream would take it at face value. Everything the
model produces passes through here first, checked against the conversation's
own project.

Pure functions: no Flask request context, no model client, no HTTP. That keeps
the security boundary testable on its own.
"""
import re
from datetime import date, datetime

from ..models.environment import Environment
from ..models.request import EnvironmentRequest

SERVICE_FIELDS = ('environment_id', 'service_ids', 'action_type', 'schedule_type',
                  'start_time', 'end_time', 'recurrence_days', 'start_hm',
                  'stop_hm', 'recur_until', 'reason')
REPO_FIELDS = ('repo_name', 'repo_description', 'repo_visibility', 'reason')

# Kept identical to _REPO_NAME_RE in blueprints/api/requests.py — these two must
# stay in step. A draft the form would reject is worse than no draft: the
# developer gets handed a prefilled form that refuses to submit.
_REPO_NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,120}$')
_HM_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

_ACTION_TYPES = {value for value, _ in EnvironmentRequest.ACTION_TYPES}
_SCHEDULE_TYPES = {'once', 'weekly'}
_VISIBILITIES = {'private', 'public'}


def validate_draft(project, request_type, raw):
    """Check a raw model draft against `project`.

    Returns (clean_draft, []) or (None, [problem, ...]). The problem strings are
    fed back to the model on the next turn so it can correct itself, so they
    name the field and what was wrong with it.
    """
    if not isinstance(raw, dict):
        return None, ['The draft was not an object.']
    if request_type == 'service':
        return _validate_service(project, raw)
    if request_type == 'repo':
        return _validate_repo(raw)
    return None, [f'Unknown request type {request_type!r}.']


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _validate_service(project, raw):
    problems = []

    env = Environment.query.filter_by(
        id=raw.get('environment_id'), project_id=project.id).first()
    if env is None:
        return None, [f'environment_id {raw.get("environment_id")!r} is not an '
                      f'environment of project {project.name}.']

    valid_service_ids = {s.id for s in env.services.all()}
    requested = raw.get('service_ids') or []
    if not isinstance(requested, list):
        problems.append('service_ids must be a list.')
        requested = []
    stray = [s for s in requested if s not in valid_service_ids]
    if stray:
        problems.append(f'service ids {stray} do not belong to environment '
                        f'{env.display_name}.')

    action_type = raw.get('action_type') or 'start_stop'
    if action_type not in _ACTION_TYPES:
        problems.append(f'action_type must be one of {sorted(_ACTION_TYPES)}.')

    schedule_type = raw.get('schedule_type') or 'once'
    if schedule_type not in _SCHEDULE_TYPES:
        problems.append(f'schedule_type must be one of {sorted(_SCHEDULE_TYPES)}.')

    start = _parse_dt(raw.get('start_time'))
    end = _parse_dt(raw.get('end_time'))
    if start is None or end is None:
        problems.append('start_time and end_time must be ISO-8601 datetimes.')
    elif end <= start:
        problems.append('end_time must be after start_time.')

    recurrence_days = start_hm = stop_hm = recur_until = None
    if schedule_type == 'weekly':
        tokens = [t.strip() for t in str(raw.get('recurrence_days') or '').split(',')
                  if t.strip()]
        unknown = [t for t in tokens if t not in EnvironmentRequest.WEEKDAYS]
        if not tokens:
            problems.append('recurrence_days is required for a weekly schedule.')
        elif unknown:
            problems.append(f'recurrence_days {unknown} are not weekday tokens '
                            f'({", ".join(EnvironmentRequest.WEEKDAYS)}).')
        else:
            recurrence_days = ','.join(tokens)

        start_hm, stop_hm = raw.get('start_hm'), raw.get('stop_hm')
        if not (_HM_RE.match(str(start_hm or '')) and _HM_RE.match(str(stop_hm or ''))):
            problems.append('start_hm and stop_hm must be HH:MM.')

        if raw.get('recur_until'):
            try:
                recur_until = date.fromisoformat(str(raw['recur_until']))
            except ValueError:
                problems.append('recur_until must be a YYYY-MM-DD date.')
            else:
                if recur_until <= date.today():
                    problems.append('recur_until must be in the future.')

    reason = (raw.get('reason') or '').strip()
    if not reason:
        problems.append('reason is required.')

    if problems:
        return None, problems

    return {
        'environment_id': env.id,
        'service_ids': list(requested),
        'action_type': action_type,
        'schedule_type': schedule_type,
        'start_time': start.isoformat(),
        'end_time': end.isoformat(),
        'recurrence_days': recurrence_days,
        'start_hm': start_hm,
        'stop_hm': stop_hm,
        'recur_until': recur_until.isoformat() if recur_until else None,
        'reason': reason,
    }, []


def _validate_repo(raw):
    problems = []

    repo_name = (raw.get('repo_name') or '').strip()
    if not _REPO_NAME_RE.match(repo_name):
        problems.append('repo_name must be 1-120 characters of letters, digits, '
                        'dots, dashes and underscores only.')

    visibility = (raw.get('repo_visibility') or 'private').strip().lower()
    if visibility not in _VISIBILITIES:
        problems.append("repo_visibility must be 'private' or 'public'.")

    description = (raw.get('repo_description') or '').strip()
    reason = (raw.get('reason') or '').strip() or description
    if not reason:
        problems.append('reason or repo_description is required.')

    if problems:
        return None, problems

    return {
        'repo_name': repo_name,
        'repo_description': description,
        'repo_visibility': visibility,
        'reason': reason,
    }, []
