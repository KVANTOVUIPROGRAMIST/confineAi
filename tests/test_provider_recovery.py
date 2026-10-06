import base64
import json

import httpx
import pytest

from app import providers
from app.config import settings
from app.schemas import Verification


def gemini_response(text='{"supported":true,"concerns":[]}', finish='STOP', tokens=(7, 3, 2)):
    return {'candidates': [{'finishReason': finish, 'content': {'parts': [{'text': text}]}}],
            'usageMetadata': {'promptTokenCount': tokens[0], 'candidatesTokenCount': tokens[1], 'thoughtsTokenCount': tokens[2]}}


def offline_provider(monkeypatch, handler):
    monkeypatch.setattr(settings, 'gemini_key', 'fake-test-credential')
    monkeypatch.setattr(settings, 'ai_provider', 'gemini')
    monkeypatch.setattr(settings, 'ai_model', 'gemini-3.5-flash-lite')
    monkeypatch.setattr(providers.time, 'sleep', lambda _: None)
    original = httpx.Client
    monkeypatch.setattr(providers.httpx, 'Client', lambda *args, **kwargs:
        original(*args, transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('first', ['output_limit', 'invalid_json', 'invalid_schema', 'empty', 'timeout', 'unavailable'])
def test_recoverable_response_retries_from_same_original_input_and_counts_all_reported_usage(monkeypatch, caplog, first):
    requests, callbacks = [], []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            if first == 'timeout':
                raise httpx.ReadTimeout('PRIVATE-CONTENT', request=request)
            if first == 'unavailable':
                return httpx.Response(503, json={'error': 'PRIVATE-CONTENT'}, request=request)
            data = gemini_response(
                text={'output_limit': '{"supported":', 'invalid_json': 'PRIVATE-CONTENT',
                      'invalid_schema': '{"supported":"not-a-bool","concerns":[]}', 'empty': ''}[first],
                finish='MAX_TOKENS' if first == 'output_limit' else 'STOP')
        else:
            data = gemini_response(tokens=(11, 5, 3))
        return httpx.Response(200, json=data, request=request)
    offline_provider(monkeypatch, handler)
    answer, tokens = providers.generate(Verification, 'Use only approved evidence.',
        'Complete original context and frozen approved evidence.', attachment=('application/pdf', b'original-private-file'),
        thinking_level='high', max_tokens=40, max_attempts=2, on_retry=lambda: callbacks.append(True))
    assert answer.supported and len(requests) == 2 and callbacks == [True]
    assert requests[0]['contents'] == requests[1]['contents']
    attachment = requests[1]['contents'][0]['parts'][1]['inlineData']
    assert base64.b64decode(attachment['data']) == b'original-private-file'
    assert tokens == ((11, 8) if first in ('timeout', 'unavailable') else (18, 13))
    assert requests[1]['generationConfig']['maxOutputTokens'] == (80 if first == 'output_limit' else 40)
    assert requests[1]['generationConfig']['thinkingConfig']['thinkingLevel'] == 'high'
    assert 'PRIVATE-CONTENT' not in caplog.text and 'fake-test-credential' not in caplog.text
    assert f'reason={first}' in caplog.text


@pytest.mark.parametrize('mode', ['output_limit', 'invalid_json'])
def test_persistent_failure_is_bounded_and_never_accepts_partial_json(monkeypatch, caplog, mode):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, request=request, json=gemini_response(
            text='PRIVATE-CONTENT', finish='MAX_TOKENS' if mode == 'output_limit' else 'STOP'))
    offline_provider(monkeypatch, handler)
    with pytest.raises(providers.ProviderFailure) as error:
        providers.generate(Verification, 'Check.', 'Original.', max_attempts=2)
    assert len(requests) == 2 and error.value.reason == mode
    assert error.value.tokens == (14, 10)
    assert 'PRIVATE-CONTENT' not in str(error.value.detail) and 'PRIVATE-CONTENT' not in caplog.text


@pytest.mark.parametrize('status', [400, 403, 429])
def test_configuration_or_billing_failures_are_not_retried(monkeypatch, status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': 'PRIVATE-CONTENT'}, request=request)
    offline_provider(monkeypatch, handler)
    with pytest.raises(providers.ProviderFailure):
        providers.generate(Verification, 'Check.', 'Original.', max_attempts=2)
    assert len(requests) == 1


def test_provider_refusal_is_not_retried_or_accepted(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, request=request, json=gemini_response(finish='SAFETY'))
    offline_provider(monkeypatch, handler)
    with pytest.raises(providers.ProviderFailure) as error:
        providers.generate(Verification, 'Check.', 'Original.', max_attempts=2)
    assert error.value.reason == 'blocked' and len(requests) == 1


def test_openai_output_limit_retries_without_dropping_original_pdf_or_schema(monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            data = {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'},
                    'usage': {'input_tokens': 4, 'output_tokens': 8}, 'output': []}
        else:
            data = {'status': 'completed', 'usage': {'input_tokens': 5, 'output_tokens': 9},
                    'output': [{'content': [{'type': 'output_text', 'text': '{"supported":true,"concerns":[]}'}]}]}
        return httpx.Response(200, json=data, request=request)
    offline_provider(monkeypatch, handler)
    monkeypatch.setattr(settings, 'ai_provider', 'openai')
    monkeypatch.setattr(settings, 'openai_key', 'fake-openai-credential')
    monkeypatch.setattr(settings, 'ai_model', 'test-model')
    answer, tokens = providers.generate(Verification, 'Check.', 'Original.', max_tokens=40,
        max_attempts=2, attachment=('application/pdf', b'original-file'))
    assert answer.supported and tokens == (9, 17)
    assert requests[0]['input'] == requests[1]['input']
    assert requests[0]['text'] == requests[1]['text']
    assert requests[1]['max_output_tokens'] == 80
