"""Retry transport failures, leaving schema/quality retries to their callers."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time

import httpx


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_s: float = 2.0
    cap_s: float = 60.0

    def __post_init__(self):
        if self.max_attempts < 1 or self.base_s < 0 or self.cap_s < 0:
            raise ValueError('invalid retry policy')


class RetryableResponse(RuntimeError):
    retryable = True


def check_response(response):
    if response.status_code >= 400:
        try:
            request = response.request
        except (AttributeError, RuntimeError):
            request = httpx.Request('GET', 'https://service.invalid')
        raise httpx.HTTPStatusError(f'HTTP {response.status_code}', request=request, response=response)
    return response


def classify(value):
    explicit = getattr(value, 'retryable', None)
    if explicit is not None:
        return 'retry' if explicit else 'fatal'
    response = getattr(value, 'response', value)
    status = getattr(response, 'status_code', None)
    if status is not None:
        return 'retry' if status in (408, 425, 429, 500, 502, 503, 504) else 'fatal'
    if isinstance(value, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                          TimeoutError, ConnectionError)):
        return 'retry'
    if getattr(value, '__cause__', None) is not None:
        return classify(value.__cause__)
    return 'fatal'


def failure_kind(exc):
    if getattr(getattr(exc, 'response', None), 'status_code', None) == 429:
        return '429'
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return 'timeout'
    return 'retry' if classify(exc) == 'retry' else 'fatal'


def retry_after(response, *, now=None):
    value = getattr(response, 'headers', {}).get('retry-after')
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - (now or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def backoff(attempt, policy):
    return random.uniform(0, min(policy.cap_s, policy.base_s * 2 ** attempt))


def with_retry(fn, policy=None, *, resource=None, sleep=None, deadline=None):
    policy = policy or RetryPolicy()
    sleep = sleep or time.sleep
    if resource:
        deadline = min(deadline or float('inf'), time.monotonic() + resource.max_wait_s)
    for attempt in range(policy.max_attempts):
        try:
            with resource.slot(deadline=deadline) if resource else nullcontext():
                return fn()
        except Exception as exc:
            if classify(exc) != 'retry' or attempt + 1 == policy.max_attempts:
                raise
            delay = retry_after(getattr(exc, 'response', None))
            delay = max(backoff(attempt, policy), delay or 0)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise
            if resource:
                resource.note_retry()
            if resource and getattr(resource, 'stop_event', None) is not None:
                from carrel.runtime.context import RunInterrupted
                if resource.stop_event.wait(delay):
                    raise RunInterrupted()
            else:
                sleep(delay)
