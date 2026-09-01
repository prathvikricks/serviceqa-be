"""Dashboard endpoints are role-scoped. Regression guard: a developer's
analytics must not 500 (the top-environments aggregate joins Environment, so
scoping has to filter on the EnvironmentRequest column, not the primary entity)."""
from conftest import login


def test_developer_analytics_loads(client, project, users):
    # 'dev' is a member of the demo project (see conftest.project fixture).
    login(client, 'dev')
    resp = client.get('/api/v1/dashboard/analytics')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert 'kpis' in body and 'top_environments' in body and 'cost_by_month' in body


def test_developer_dashboard_loads(client, project, users):
    login(client, 'dev')
    resp = client.get('/api/v1/dashboard')
    assert resp.status_code == 200
    assert 'stats' in resp.get_json()


def test_admin_analytics_loads(client, project, users):
    login(client, 'admin')
    assert client.get('/api/v1/dashboard/analytics').status_code == 200
