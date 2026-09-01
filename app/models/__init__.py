"""Model package — importing this registers every table with SQLAlchemy."""
from .user import Role, User, ProjectMember  # noqa: F401
from .project import Project  # noqa: F401
from .environment import Environment, CloudService  # noqa: F401
from .request import EnvironmentRequest, RequestService, ScheduledJob  # noqa: F401
from .approval import Approval  # noqa: F401
from .audit import AuditLog  # noqa: F401
from .budget import CostRecord  # noqa: F401
from .secret import ProjectSecret  # noqa: F401
from .project_aws_secret import ProjectAwsSecret  # noqa: F401
from .chat import ChatConversation, ChatMessage  # noqa: F401
from .ticket import Ticket, TicketComment  # noqa: F401
from .email_intake import EmailIntakeMessage  # noqa: F401
from .setting import Setting, get_setting  # noqa: F401
from .vulnerability import Vulnerability, VulnSourceStatus  # noqa: F401

__all__ = [
    'Role', 'User', 'ProjectMember', 'Project', 'Environment', 'CloudService',
    'EnvironmentRequest', 'RequestService', 'ScheduledJob', 'Approval',
    'AuditLog', 'CostRecord', 'ProjectSecret', 'ProjectAwsSecret', 'ChatConversation',
    'ChatMessage', 'Ticket', 'TicketComment', 'EmailIntakeMessage', 'Setting', 'get_setting',
    'Vulnerability', 'VulnSourceStatus',
]
