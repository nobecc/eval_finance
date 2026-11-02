"""Direct OpenAI-compatible judge transport and strict judgment parsing.

Only the judge's machine-readable response is JSON. Candidate reports are
untrusted free text and are never validated against a report schema.
"""
from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import EvaluationError






def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError(f'duplicate judge JSON key: {key}')
        result[key] = value
    return result


def parse_json(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith('```') and content.endswith('```'):
        content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    def reject(value: str) -> None:
        raise EvaluationError(f'invalid JSON constant: {value}')
    try:
        value = json.loads(content, object_pairs_hook=_pairs, parse_constant=reject)
    except (ValueError, IndexError) as exc:
        raise EvaluationError(f'invalid judge JSON: {exc}') from exc
    if not isinstance(value, dict):
        raise EvaluationError('judge response must be one JSON object')
    return value






class JudgeClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        if not config['base_url'].strip() or not config['model'].strip():
            raise EvaluationError('judge base_url and model are required')
        base = config['base_url'].rstrip('/')
        self.url = base if base.endswith('/chat/completions') else base + '/chat/completions'

    def ask(self, system: str, user: str, validate: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        cfg = self.config
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        error = ''
        response_format = True
        max_tokens = cfg['max_tokens']
        token_limit = cfg.get('max_tokens_limit', max_tokens)
        started = time.monotonic()
        deadline = started + cfg.get('total_timeout_s', cfg['timeout_s'] * (cfg['max_retries'] + 1) + 2 * cfg['max_retries'])
        history: list[dict[str, Any]] = []
        for attempt in range(cfg['max_retries'] + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = 'judge total timeout exhausted'
                break
            request_timeout = min(cfg['timeout_s'] * max_tokens / cfg['max_tokens'], remaining)
            entry = {'attempt': attempt + 1, 'max_tokens': max_tokens, 'timeout_s': request_timeout}
            history.append(entry)
            payload: dict[str, Any] = {'model': cfg['model'], 'messages': messages,
                                       'temperature': 0, 'max_tokens': max_tokens}
            if response_format:
                payload['response_format'] = {'type': 'json_object'}
            headers = {'Content-Type': 'application/json'}
            if cfg['api_key']:
                headers['Authorization'] = 'Bearer ' + cfg['api_key']
            request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
            try:
                # No inherited proxy, no agent Gateway, no shared httpx dependency.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=request_timeout) as response:
                    body = json.load(response)
                choice = body['choices'][0]
                if not isinstance(choice,dict):
                    raise EvaluationError('judge choice must be an object')
                message = choice.get('message') or {}
                if not isinstance(message,dict):
                    raise EvaluationError('judge message must be an object')
                content = message.get('content')
                reasoning = message.get('reasoning_content', message.get('reasoning'))
                entry['content_chars'] = len(content) if isinstance(content, str) else 0
                entry['reasoning_chars'] = len(reasoning) if isinstance(reasoning, str) else 0
                usage = body.get('usage') or {}
                if isinstance(usage, dict):
                    entry['usage'] = {key:usage[key] for key in ['prompt_tokens','completion_tokens','total_tokens']
                                      if type(usage.get(key)) is int}
                finish_reason = choice.get('finish_reason')
                entry['finish_reason'] = finish_reason
                if finish_reason == 'content_filter':
                    error = 'judge response stopped with content_filter'
                    entry['error'] = error
                    break
                if finish_reason == 'length':
                    error = 'judge response stopped with length'
                    entry['error'] = error
                    if max_tokens >= token_limit:
                        # Repeating a truncated judgment with the same cap wastes
                        # time. Never accept partial labels as a complete judgment.
                        break
                    max_tokens = min(token_limit, max_tokens * 2)
                    messages = [*messages[:2], {'role': 'user', 'content':
                        'The previous response hit the generation limit. Use brief reasoning and return the complete compact JSON object with every requested key. Do not omit any decision.'}]
                else:
                    content = choice['message']['content']
                    if not isinstance(content, str):
                        raise EvaluationError('judge returned no text content')
                    value = validate(parse_json(content))
                    return {**value, '_attempts': len(history), '_attempt_history': history}
            except urllib.error.HTTPError as exc:
                error = f'HTTP {exc.code}'
                entry['error'] = error
                if exc.code == 400 and response_format:
                    response_format = False
                elif exc.code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                    break
            except (OSError, http.client.HTTPException) as exc:
                error = f'{type(exc).__name__}: {exc}'[:400]
                if cfg['api_key']:
                    error = error.replace(cfg['api_key'], '[redacted]')
                entry['error'] = error
                # Transport errors retry the original judgment, without treating
                # the connection failure as evidence about the candidate answer.
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                error = f'{type(exc).__name__}: {exc}'[:400]
                if cfg['api_key']:
                    error = error.replace(cfg['api_key'], '[redacted]')
                entry['error'] = error
                messages = [*messages[:2], {'role': 'user', 'content':
                    'The previous judge response was invalid: ' + error + '. Return only the requested compact JSON object.'}]
            if attempt < cfg['max_retries']:
                time.sleep(min(2.0, 0.25 * 2 ** attempt, max(0.0, deadline - time.monotonic())))
        return {'context_quality': 'API_FAILED', 'error': error, '_attempts': len(history), '_attempt_history': history}
