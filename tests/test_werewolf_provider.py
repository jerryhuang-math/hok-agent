import httpx
import pytest

from agent.providers import OpenAIProvider, ProviderError


@pytest.mark.parametrize('status,code,hint', [
    (429, 'rate_limit_exceeded', 'rate limit reached'),
    (429, 'insufficient_quota', 'quota exhausted'),
    (401, 'invalid_api_key', 'API key was rejected'),
    (404, 'model_not_found', 'model unavailable'),
    (400, 'invalid_json_schema', 'structured-output schema'),
    (400, 'unsupported_parameter', 'unsupported request parameter'),
])
def test_api_errors_have_actionable_safe_hints(status, code, hint):
    secret = 'sk-test-do-not-log'
    def handler(request):
        return httpx.Response(status, json={'error': {'code': code, 'message': secret}})
    provider = OpenAIProvider(secret, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        provider.complete_structured([], 'test', {})
    assert hint in str(caught.value)
    assert secret not in str(caught.value)
    assert not caught.value.retryable


def test_unknown_error_body_never_logged():
    provider = OpenAIProvider('sk-secret', transport=httpx.MockTransport(
        lambda request: httpx.Response(500, json={'error': {'code': 'sk-secret', 'message': 'private prompt'}})))
    with pytest.raises(ProviderError) as caught:
        provider.complete_structured([], 'test', {})
    assert 'sk-secret' not in str(caught.value) and 'private prompt' not in str(caught.value)
    assert caught.value.retryable


def test_daily_limit_diagnostics_extract_only_metric_and_numbers():
    message = 'Rate limit reached for sk-secret in organization org-private on requests per day (RPD): Limit 50, Used 50, Requested 1. Please try again.'
    provider = OpenAIProvider('sk-secret', transport=httpx.MockTransport(
        lambda request: httpx.Response(429, headers={'Retry-After': '1728'},
            json={'error': {'code': 'rate_limit_exceeded', 'message': message}})))
    with pytest.raises(ProviderError) as caught:
        provider.complete_structured([], 'test', {})
    text = str(caught.value)
    assert 'requests per day' in text and 'limit=50' in text and 'used=50' in text
    assert 'retry_after_seconds=1728' in text
    assert 'sk-secret' not in text and 'org-private' not in text


@pytest.mark.parametrize('model,planning,effort,budget', [
    ('gpt-6-luna', True, 'low', 8192),
    ('gpt-6-luna', False, 'none', 2048),
    ('gpt-4o-mini', True, None, 8192),
])
def test_live_clock_request_controls(model, planning, effort, budget):
    requests = []
    def handler(request):
        import json
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]})
    provider = OpenAIProvider('sk-test', model, transport=httpx.MockTransport(handler))
    provider.complete_structured([], 'test', {'properties': {'vote_order': {}} if planning else {}}, timeout=35)
    assert requests[0]['max_completion_tokens'] == budget
    assert requests[0].get('reasoning_effort') == effort
