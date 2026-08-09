"""
Cost estimation for environment requests.
"""


def estimate_request_cost(environment, start_time, end_time):
    """Calculate estimated cost for running an environment for a given duration."""
    duration_hours = (end_time - start_time).total_seconds() / 3600
    hourly_cost = environment.total_hourly_cost
    return round(hourly_cost * duration_hours, 2)


def format_cost(cost):
    """Format cost as USD string."""
    return f"${cost:.2f}"
