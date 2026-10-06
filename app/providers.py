import base64
import json
import logging
import time

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from .config import settings

log = logging.getLogger(__name__)


class ProviderFailure(HTTPException):
    """Safe diagnostics and reported usage; never retain provider text or secrets."""
    def __init__(self, reason, detail, tokens=(0, 0), retryable=False):
        super().__init__(502, detail)
        self.reason = reason
        self.tokens = tokens
        self.retryable = retryable


FAILURES = {
    'timeout': 'The AI provider timed out while processing this step.',
    'transport': 'The connection to the AI provider was interrupted.',
    'output_limit': 'The AI provider reached its response limit before finishing this step.',
    'invalid_json': 'The AI provider returned an incomplete or malformed document response.',
    'invalid_schema': 'The AI provider returned a document in an unexpected format.',
    'empty': 'The AI provider returned no usable document response.',
    'blocked': 'The AI provider could not complete this document request.',
    'unavailable': 'The AI provider is temporarily unavailable.',
    'rate_limit': 'The AI provider has reached its rate or billing limit. Please retry later.',
    'access': 'The AI connection needs attention. Check the server API key and model access.',
    'request': 'The AI request could not run. Check that the configured model supports structured output and file inputs.',
}


def failure(reason, schema, tokens=(0, 0), retryable=False):
    # Only fixed diagnostic codes, code-owned schema names and numeric usage enter logs.
    log.warning('AI response failed reason=%s schema=%s input_tokens=%d output_tokens=%d',
                reason, schema.__name__, *tokens)
    return ProviderFailure(reason, FAILURES[reason] + ' Your response allowance is restored if the request cannot finish.',
                           tokens, retryable)


def gemini_schema(node):
    # Bounds on nested arrays can exceed Gemini's schema-complexity budget.
    # Pydantic still enforces every original bound on the returned response.
    if isinstance(node, dict):
        return {key: gemini_schema(value) for key, value in node.items() if key not in ('minItems', 'maxItems')}
    if isinstance(node, list):
        return [gemini_schema(value) for value in node]
    return node


def usage_tokens(usage, provider):
    def count(key):
        value = usage.get(key, 0)
        return value if isinstance(value, int) and 0 <= value <= 1_000_000_000 else 0
    if provider == 'gemini':
        return count('promptTokenCount'), count('candidatesTokenCount') + count('thoughtsTokenCount')
    return count('input_tokens'), count('output_tokens')


def generate(schema, instructions, content, max_tokens=5000, attachment=None, thinking_level=None,
             max_attempts=1, on_retry=None):
    """Bounded recovery from the same input; no browsing, retrieval or code tools."""
    if not settings.ai_ready:
        raise HTTPException(503, 'Live AI is not connected yet. Set GEMINI_API_KEY or OPENAI_API_KEY on the server. Your files and course setup still work.')
    if settings.ai_provider not in ('gemini', 'openai'):
        raise HTTPException(503, 'Unsupported AI provider. Choose gemini or openai in the server configuration.')
    total = [0, 0]
    attempts = max(1, min(2, max_attempts))
    for attempt in range(attempts):
        try:
            result, tokens = generate_once(schema, instructions, content, max_tokens, attachment, thinking_level)
            return result, (total[0] + tokens[0], total[1] + tokens[1])
        except ProviderFailure as exc:
            total[0] += exc.tokens[0]
            total[1] += exc.tokens[1]
            if not exc.retryable or attempt + 1 == attempts:
                exc.tokens = tuple(total)
                raise
            if exc.reason == 'output_limit':
                max_tokens = min(max_tokens * 2, 48_000)
            if on_retry:
                on_retry()
            # Regenerate from the complete original input; never accept or continue partial JSON.
            instructions += '\nReturn a complete response matching the requested JSON schema exactly. Preserve every required task and all evidence restrictions. Keep explanations concise, without omitting required working. Do not output Markdown fences around the JSON.'
            time.sleep(1)


def generate_once(schema, instructions, content, max_tokens, attachment, thinking_level):
    tokens = (0, 0)
    try:
        # Full-file reasoning can exceed the short tutoring timeout.
        timeout = 180 if attachment or thinking_level else 120
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=15)) as client:
            if settings.ai_provider == 'gemini':
                generation_config = {'responseMimeType': 'application/json',
                                     'responseJsonSchema': gemini_schema(schema.model_json_schema()),
                                     'maxOutputTokens': max_tokens}
                if thinking_level and settings.ai_model.startswith('gemini-3'):
                    generation_config['thinkingConfig'] = {'thinkingLevel': thinking_level}
                parts = [{'text': content}]
                if attachment:
                    parts.append({'inlineData': {'mimeType': attachment[0], 'data': base64.b64encode(attachment[1]).decode()}})
                response = client.post(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{settings.ai_model}:generateContent',
                    headers={'x-goog-api-key': settings.gemini_key},
                    json={'systemInstruction': {'parts': [{'text': instructions}]},
                          'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': generation_config})
                response.raise_for_status()
                data = response.json()
                tokens = usage_tokens(data.get('usageMetadata', {}), 'gemini')
                candidates = data.get('candidates', [])
                if not candidates:
                    if data.get('promptFeedback', {}).get('blockReason'):
                        raise failure('blocked', schema, tokens)
                    raise failure('empty', schema, tokens, True)
                finish = candidates[0].get('finishReason')
                if finish == 'MAX_TOKENS':
                    raise failure('output_limit', schema, tokens, True)
                if finish not in (None, 'STOP'):
                    raise failure('blocked', schema, tokens)
                result = ''.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', []) if not p.get('thought'))
            else:
                parts = [{'type': 'input_text', 'text': content}]
                if attachment:
                    encoded = base64.b64encode(attachment[1]).decode()
                    if attachment[0] == 'application/pdf':
                        parts.append({'type': 'input_file', 'filename': 'upload.pdf',
                                      'file_data': 'data:application/pdf;base64,' + encoded})
                    else:
                        parts.append({'type': 'input_image', 'image_url': f'data:{attachment[0]};base64,{encoded}'})
                response = client.post('https://api.openai.com/v1/responses',
                    headers={'Authorization': 'Bearer ' + settings.openai_key},
                    json={'model': settings.ai_model, 'store': False, 'instructions': instructions,
                          'input': [{'role': 'user', 'content': parts}], 'max_output_tokens': max_tokens,
                          'text': {'format': {'type': 'json_schema', 'name': schema.__name__, 'strict': True, 'schema': schema.model_json_schema()}}})
                response.raise_for_status()
                data = response.json()
                tokens = usage_tokens(data.get('usage', {}), 'openai')
                if data.get('status') != 'completed':
                    if (data.get('incomplete_details') or {}).get('reason') == 'max_output_tokens':
                        raise failure('output_limit', schema, tokens, True)
                    raise failure('blocked', schema, tokens)
                result = ''.join(part.get('text', '') for item in data.get('output', [])
                                 for part in item.get('content', []) if part.get('type') == 'output_text')
            if not result.strip():
                raise failure('empty', schema, tokens, True)
        # Never relax schema bounds or source/coverage checks to rescue an invalid response.
        return schema.model_validate_json(result), tokens
    except ProviderFailure:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        reason = 'rate_limit' if status == 429 else 'access' if status in (401, 403) else 'request' if status in (400, 404) else 'unavailable'
        raise failure(reason, schema, tokens, status in (408, 500, 502, 503, 504)) from None
    except httpx.TimeoutException:
        raise failure('timeout', schema, tokens, True) from None
    except httpx.HTTPError:
        raise failure('transport', schema, tokens, True) from None
    except ValidationError as exc:
        reason = 'invalid_json' if any(e['type'] == 'json_invalid' for e in exc.errors(include_input=False)) else 'invalid_schema'
        raise failure(reason, schema, tokens, True) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise failure('invalid_json', schema, tokens, True) from None
