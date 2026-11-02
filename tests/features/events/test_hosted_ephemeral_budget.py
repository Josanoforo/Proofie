"""Page GET rate proof belongs to the published events namespace; no network."""
from _hosted import authenticated_client
from copilot.hosted_ephemeral import create_app
from test_hosted_ephemeral import configured, isolate_factory_environment_and_template_notice  # noqa: F401


def test_ephemeral_page_gets_use_shared_rate_budget_without_post(monkeypatch, tmp_path):
    from _hosted import authenticated_client
    import copilot.features.events.rate as rate
    from copilot.web.routes_disputes import get_client
    configured(monkeypatch, tmp_path)
    monkeypatch.setenv('COPILOT_FEATURE_DEADLINES', 'true')
    monkeypatch.setenv('COPILOT_FEATURE_EVENTS', 'true')
    budget = rate.Budget(clock=lambda: 0.0)
    monkeypatch.setattr(rate, 'SHARED_BUDGET', budget)
    calls = []

    class Fake:
        def call(self, method, path, op, **kwargs):
            assert method == 'GET'
            calls.append((method, path, op))
            return {'items': [], 'links': []}

    app = create_app()
    app.dependency_overrides[get_client] = lambda: Fake()
    client = authenticated_client(app)
    assert client.get('/events/state').status_code == 200
    assert not calls  # Local state page consumes no PayPal GET.
    assert [client.get('/deadlines').status_code for _ in range(45)] == [200] * 45
    assert len(calls) == 45 and len(budget.stamps) == 45
    response = client.get('/deadlines')
    assert response.status_code == 503 and 'Try again in a minute with Refresh from PayPal' in response.text
    assert len(calls) == 45  # The 46th page load never reaches the fake transport.
    assert all(method == 'GET' for method, _, _ in calls)
    print('L54: 47 HTTP page GETs = local state1 + deadlines45 + deferred1; owned fake GETs45, POST0')
