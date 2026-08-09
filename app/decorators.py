from functools import wraps
from flask import abort
from flask_login import current_user


def role_required(*roles):
    """Restrict access to users with specific roles."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role.name not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def admin_required(f):
    """Restrict access to admin users."""
    return role_required('admin')(f)


def devops_required(f):
    """Restrict access to devops and admin users."""
    return role_required('devops', 'admin')(f)


def project_access_required(f):
    """Ensure user has access to the project referenced by project_id in kwargs."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        project_id = kwargs.get('project_id')
        if project_id and not current_user.is_member_of(project_id):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function
