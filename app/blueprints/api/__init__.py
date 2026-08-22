from flask import Blueprint

api_bp = Blueprint('api', __name__)

# Imported for their side effect of registering routes on api_bp.
from . import auth       # noqa: E402, F401
from . import routes     # noqa: E402, F401
from . import dashboard  # noqa: E402, F401
from . import requests   # noqa: E402, F401
from . import approvals  # noqa: E402, F401
from . import admin      # noqa: E402, F401
from . import reports     # noqa: E402, F401
from . import secrets     # noqa: E402, F401
from . import chat        # noqa: E402, F401
