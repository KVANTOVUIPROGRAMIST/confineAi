import base64
import json

import httpx
from fastapi import HTTPException

from .config import settings


def generate(schema, instructions, content, max_tokens=5000, attachment=None):
    """No browsing tools, URL context, code execution, or external retrieval are enabled."""
    if not settings.ai_ready:
        raise HTTPException(503, 'Live AI is not connected yet. Set GEMINI_API_KEY or OPENAI_API_KEY on the server. Your files and course setup still work.')
    if settings.ai_provider not in ('gemini', 'openai'):
        raise HTTPException(503, 'Unsupported AI provider. Choose gemini or openai in the server configuration.')
    json_schema = schema.model_json_schema()
    try:
        with httpx.Client(timeout=httpx.Timeout(120, connect=15)) as client:
            if settings.ai_provider == 'gemini':
                parts = [{'text': content}]
                if attachment:
                    parts.append({'inlineData': {'mimeType': attachment[0], 'data': base64.b64encode(attachment[1]).decode()}})
                response = client.post(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{settings.ai_model}:generateContent',
                    headers={'x-goog-api-key': settings.gemini_key},
                    json={'systemInstruction': {'parts': [{'text': instructions}]},
                          'contents': [{'role': 'user', 'parts': parts}],
                          'generationConfig': {'responseMimeType': 'application/json', 'responseJsonSchema': json_schema,
                                               'maxOutputTokens': max_tokens}})
                response.raise_for_status()
                data = response.json()
                candidates = data.get('candidates', [])
                if not candidates or candidates[0].get('finishReason') not in (None, 'STOP'):
                    raise ValueError('Incomplete or blocked response')
                result = ''.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', []) if not p.get('thought'))
                usage = data.get('usageMetadata', {})
                tokens = (usage.get('promptTokenCount', 0), usage.get('candidatesTokenCount', 0) + usage.get('thoughtsTokenCount', 0))
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
                          'text': {'format': {'type': 'json_schema', 'name': schema.__name__, 'strict': True, 'schema': json_schema}}})
                response.raise_for_status()
                data = response.json()
                if data.get('status') != 'completed':
                    raise ValueError('Incomplete response')
                result = ''.join(part.get('text', '') for item in data.get('output', [])
                                 for part in item.get('content', []) if part.get('type') == 'output_text')
                usage = data.get('usage', {})
                tokens = (usage.get('input_tokens', 0), usage.get('output_tokens', 0))
        return schema.model_validate_json(result), tokens
    except httpx.HTTPStatusError as exc:
        # Never echo provider response bodies or credentials into client errors.
        status = exc.response.status_code
        message = 'The AI provider is temporarily unavailable. Please retry.'
        if status == 429:
            message = 'The AI provider has reached its rate or billing limit. Please retry later.'
        elif status in (401, 403):
            message = 'The AI connection needs attention. Check the server API key and model access.'
        elif status in (400, 404):
            message = 'The AI request could not run. Check that the configured model supports structured output and file inputs.'
        raise HTTPException(502, message) from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, 'The AI response could not be validated. Please retry; your response allowance is restored.') from None
