from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from carrel.runtime.retry import RetryPolicy, backoff, classify, retry_after, with_retry


@pytest.mark.parametrize('status', [408, 425, 429, 500, 502, 503, 504])
def test_retryable_status(status):
    assert classify(httpx.Response(status)) == 'retry'


@pytest.mark.parametrize('status', [400, 401, 403, 404, 413, 422])
def test_fatal_status(status):
    assert classify(httpx.Response(status)) == 'fatal'


def test_retry_after_seconds_and_http_date():
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert retry_after(httpx.Response(429, headers={'Retry-After': '2.5'})) == 2.5
    assert retry_after(httpx.Response(429, headers={'Retry-After': format_datetime(now + timedelta(seconds=31))}), now=now) == 31
    assert retry_after(httpx.Response(429, headers={'Retry-After': 'bad'})) is None


def test_jitter_and_original_exception():
    policy = RetryPolicy(4, 2, 7)
    for attempt in range(10):
        assert 0 <= backoff(attempt, policy) <= min(7, 2 * 2 ** attempt)
    error = httpx.ReadTimeout('original timeout')
    calls = []
    def call():
        calls.append(1)
        raise error
    with pytest.raises(httpx.ReadTimeout) as caught:
        with_retry(call, policy, sleep=lambda seconds: None)
    assert caught.value is error and len(calls) == 4
    assert classify(ConnectionResetError()) == 'retry'


def test_fatal_never_sleeps():
    def call():
        raise ValueError('bad input')
    with pytest.raises(ValueError):
        with_retry(call, sleep=lambda _: pytest.fail('fatal must not retry'))
