"""The model proposes; the server decides.

Every id, enum and date in a model-produced draft is re-checked against the
conversation's own project before it can reach a form. A draft that names a
resource the developer cannot see must not survive.
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.environment import Environment, CloudService
from app.models.project import Project
from app.services.chat_validation import validate_draft


def _service_raw(env_id, service_ids, **over):
    start = datetime.now() + timedelta(days=1)
    raw = {
        'environment_id': env_id,
        'service_ids': service_ids,
        'action_type': 'start_stop',
        'schedule_type': 'once',
        'start_time': start.replace(microsecond=0).isoformat(),
        'end_time': (start + timedelta(hours=8)).replace(microsecond=0).isoformat(),
        'reason': 'client demo',
    }
    raw.update(over)
    return raw


def test_a_good_service_draft_passes(project):
    env = project.environments.first()
    ids = [s.id for s in env.services.all()]

    clean, problems = validate_draft(project, 'service', _service_raw(env.id, ids))

    assert problems == []
    assert clean['environment_id'] == env.id
    assert sorted(clean['service_ids']) == sorted(ids)


def test_an_environment_from_another_project_is_rejected(project, users):
    other = Project(name='Other', slug='other', cloud_provider='aws', mode='mock',
                    created_by=users['admin'].id)
    other.set_provider_config({'region': 'us-east-1'})
    db.session.add(other)
    db.session.flush()
    foreign_env = Environment(project_id=other.id, name='dev', display_name='Dev')
    db.session.add(foreign_env)
    db.session.commit()

    clean, problems = validate_draft(project, 'service', _service_raw(foreign_env.id, []))

    assert clean is None
    assert any('environment' in p for p in problems)


def test_a_service_from_another_environment_is_rejected(project, users):
    env = project.environments.first()
    other_env = Environment(project_id=project.id, name='dev', display_name='Dev')
    db.session.add(other_env)
    db.session.flush()
    stray = CloudService(environment_id=other_env.id, name='Stray',
                         service_type='ec2', cloud_resource_id='i-stray',
                         hourly_cost=0.1, current_status='stopped')
    db.session.add(stray)
    db.session.commit()

    clean, problems = validate_draft(project, 'service', _service_raw(env.id, [stray.id]))

    assert clean is None
    assert any('service' in p for p in problems)


def test_an_unknown_action_type_is_rejected(project):
    env = project.environments.first()
    clean, problems = validate_draft(
        project, 'service', _service_raw(env.id, [], action_type='obliterate'))
    assert clean is None


def test_an_end_before_its_start_is_rejected(project):
    env = project.environments.first()
    start = datetime.now() + timedelta(days=1)
    clean, problems = validate_draft(project, 'service', _service_raw(
        env.id, [],
        start_time=start.isoformat(),
        end_time=(start - timedelta(hours=1)).isoformat()))
    assert clean is None


def test_bad_weekday_tokens_are_rejected(project):
    env = project.environments.first()
    clean, problems = validate_draft(project, 'service', _service_raw(
        env.id, [], schedule_type='weekly', recurrence_days='mon,funday',
        start_hm='09:00', stop_hm='17:00'))
    assert clean is None


def test_a_past_recur_until_is_rejected(project):
    env = project.environments.first()
    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
    clean, problems = validate_draft(project, 'service', _service_raw(
        env.id, [], schedule_type='weekly', recurrence_days='mon',
        start_hm='09:00', stop_hm='17:00', recur_until=yesterday))
    assert clean is None


def test_a_good_repo_draft_passes(project):
    clean, problems = validate_draft(project, 'repo', {
        'repo_name': 'billing-service',
        'repo_description': 'Billing API',
        'repo_visibility': 'private',
        'reason': 'new service',
    })
    assert problems == []
    assert clean['repo_name'] == 'billing-service'


def test_an_invalid_repo_name_is_rejected(project):
    clean, problems = validate_draft(project, 'repo', {
        'repo_name': 'not a valid name!',
        'repo_visibility': 'private',
        'reason': 'x',
    })
    assert clean is None


def test_an_unknown_request_type_is_rejected(project):
    clean, problems = validate_draft(project, 'wormhole', {})
    assert clean is None
