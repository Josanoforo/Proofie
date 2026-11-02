"""Hosted mode, offline: configuration and a fake single webhook POST."""
import pytest
from copilot import config


def test_local_binding_ignores_hosted_environment(monkeypatch):
    monkeypatch.delenv('COPILOT_HOSTED_DEMO', raising=False)
    monkeypatch.setenv('COPILOT_HOST', '0.0.0.0')
    monkeypatch.setenv('PORT', 'invalid')
    assert config.server_host() == '127.0.0.1'
    assert config.server_port() == 8000


def test_hosted_binding_reads_environment_fresh(monkeypatch):
    monkeypatch.setenv('COPILOT_HOSTED_DEMO', 'true')
    monkeypatch.setenv('COPILOT_HOST', '127.0.0.1')
    monkeypatch.setenv('PORT', '8123')
    assert config.server_host() == '127.0.0.1'
    assert config.server_port() == 8123
    monkeypatch.setenv('PORT', '8124')
    assert config.server_port() == 8124
    for value in ('no', '0', '65536'):
        monkeypatch.setenv('PORT', value)
        with pytest.raises(ValueError):
            config.server_port()


def test_hosted_manual_guard_precedes_model(monkeypatch):
    monkeypatch.setenv('COPILOT_HOSTED_DEMO', 'true')
    with pytest.raises(RuntimeError, match='AI assistant is switched off'):
        config.refuse_model_if_hosted()


def test_webhook_registration_one_fake_post():
    from tools.register_webhook import register
    calls = []
    class Fake:
        def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {'id': 'SYNTHETIC-WEBHOOK'}
    assert register(Fake(), 'https://judge.invalid/webhooks/paypal') == 'SYNTHETIC-WEBHOOK'
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ('POST', '/v1/notifications/webhooks', 'register_webhook')
    assert kwargs['json_body'] == {
        'url': 'https://judge.invalid/webhooks/paypal',
        'event_types': [{'name': name} for name in (
            'CUSTOMER.DISPUTE.CREATED', 'CUSTOMER.DISPUTE.UPDATED', 'CUSTOMER.DISPUTE.RESOLVED')],
    }


@pytest.mark.parametrize('url', ['http://judge.invalid/x', 'https://user:pass@personal.example.com/x',
                                  'https://judge.invalid/x?token=fake', 'https://judge.invalid/x#fragment'])
def test_invalid_webhook_url_never_calls_client(url):
    from tools.register_webhook import register
    class Forbidden:
        def call(self, *args, **kwargs):
            raise AssertionError('invalid URL reached client')
    with pytest.raises(ValueError):
        register(Forbidden(), url)
