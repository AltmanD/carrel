from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx
import pytest

from carrel.runtime.context import priority, submit
from carrel.runtime.limits import ResourcePool
from carrel.runtime.retry import RetryPolicy, with_retry


def test_limit_and_priority_order():
    pool = ResourcePool('test', 1)
    order = []
    def run(n):
        with pool.slot(priority=n):
            assert pool.active == 1
            order.append(n)
    with ThreadPoolExecutor(max_workers=4) as workers:
        with pool.slot():
            futures = [workers.submit(run, n) for n in (3, 1, 2)]
            deadline = time.monotonic() + 2
            while pool.snapshot()['waiting'] < 3 and time.monotonic() < deadline:
                time.sleep(.005)
            assert pool.snapshot()['waiting'] == 3
        for future in futures:
            future.result(timeout=2)
    assert order == [1, 2, 3]


def test_retry_sleep_releases_slot():
    pool = ResourcePool('retry', 1)
    seen = []
    def wait(seconds):
        with pool.slot():
            seen.append(pool.active)
        return False
    pool.stop_event.wait = wait
    count = 0
    def request():
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout('timeout')
        return 'ok'
    assert with_retry(request, RetryPolicy(2, .001), resource=pool) == 'ok'
    assert seen == [1] and pool.active == 0


def test_circuit_single_half_open_probe_and_recovery():
    pool = ResourcePool('outage', 3, breaker_failures=2, cooldown_s=.06)
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            with pool.slot():
                raise httpx.ConnectError('offline')
    assert pool.snapshot()['state'] == 'open'
    gate, entered = threading.Event(), threading.Event()
    count = []
    def call():
        with pool.slot():
            count.append(1)
            entered.set()
            assert gate.wait(2)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(call) for _ in range(3)]
        assert entered.wait(2)
        assert pool.snapshot()['state'] == 'half_open'
        assert len(count) == 1
        gate.set()
        for future in futures:
            future.result(timeout=2)
    assert pool.snapshot()['state'] == 'closed'
    assert pool.breaker_count == 1


def test_aimd_recovers_gradually():
    clock = [1.]
    pool = ResourcePool('llm', 12, adaptive=True, clock=lambda: clock[0])
    pool.report(False, '429'); pool.report(False, '429')
    assert pool.limit == 8
    clock[0] += 61
    pool.report(True)
    assert pool.limit == 9


def test_priority_reaches_nested_module_threads():
    token = priority.set(7)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert submit(pool, priority.get).result(timeout=2) == 7
    finally:
        priority.reset(token)
