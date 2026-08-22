"""Approval rights are scoped to the project a request belongs to.

Membership says *who is on* a project; `project_role` says *what they are* on
it. Only a project-devops (or an admin) may approve that project's requests.
"""
from app.extensions import db
from app.models.user import ProjectMember

from conftest import login, make_user


def _member(project, user, project_role='developer'):
    """Add `user` to `project` with the given project role, or update it."""
    existing = project.members.filter_by(user_id=user.id).first()
    if existing:
        existing.project_role = project_role
        db.session.commit()
        return existing
    m = ProjectMember(project_id=project.id, user_id=user.id,
                      added_by=user.id, project_role=project_role)
    db.session.add(m)
    db.session.commit()
    return m


def test_membership_defaults_to_developer(project, users):
    member = project.members.filter_by(user_id=users['dev'].id).first()
    assert member.project_role == 'developer'


def test_project_devops_predicate(project, users):
    ops = make_user('ops2', 'devops')
    # A global devops with no project role does not gain approval rights.
    assert ops.is_project_devops(project.id) is False

    _member(project, ops, 'devops')
    assert ops.is_project_devops(project.id) is True


def test_developer_membership_grants_no_approval_rights(project, users):
    assert users['dev'].is_project_devops(project.id) is False


def test_admin_is_always_a_project_approver(project, users):
    assert users['admin'].is_project_devops(project.id) is True
