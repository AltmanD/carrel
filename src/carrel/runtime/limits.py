"""Priority admission, paced requests, adaptive limits and circuit breaking."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import itertools
import math
import threading
import time

from carrel.runtime.context import priority as run_priority, RunInterrupted
from carrel.runtime.retry import failure_kind


class ResourceUnavailable(RuntimeError):
    retryable = True
    code = 'service_unavailable'


class ResourcePool:
    def __init__(self, name, maximum, *, minimum=1, interval_s=0, adaptive=False,
                 breaker_failures=5, cooldown_s=30, max_cooldown_s=600,
                 max_wait_s=1800, recovery_s=60, clock=time.monotonic, stop_event=None):
        if not 1 <= minimum <= maximum or interval_s < 0:
            raise ValueError('invalid resource limits')
        self.name, self.maximum, self.minimum = name, maximum, minimum
        self.limit = maximum
        self.active = 0
        self.adaptive, self.interval_s = adaptive, interval_s
        self.breaker_failures = breaker_failures
        self.cooldown_s, self.max_cooldown_s = cooldown_s, max_cooldown_s
        self.max_wait_s, self.recovery_s = max_wait_s, recovery_s
        self._clock = clock
        self.stop_event = stop_event or threading.Event()
        self._cv = threading.Condition()
        self._queue = []
        self._sequence = itertools.count()
        self._failures = deque()
        self._open_until = 0
        self._half_open = False
        self._outages = 0
        self._next_request = 0
        self._stable_since = clock()
        self._pressure = 0
        self.breaker_count = self.retries = self.requests = 0

    @contextmanager
    def slot(self, priority=None, *, deadline=None):
        with self._cv:
            ticket = (run_priority.get() if priority is None else priority, next(self._sequence))
            self._queue.append(ticket)
            started = self._clock()
            probe = False
            try:
                while True:
                    if self.stop_event.is_set():
                        raise RunInterrupted()
                    now = self._clock()
                    if now - started >= self.max_wait_s or (deadline is not None and now >= deadline):
                        raise ResourceUnavailable(f'{self.name} 持续不可用，已达到等待时限')
                    available = self.active < self.limit and ticket == min(self._queue)
                    if available and now >= self._next_request and now >= self._open_until and not self._half_open:
                        if self._open_until:
                            # Outstanding calls from the old generation must drain.
                            if self.active:
                                self._cv.wait(.1)
                                continue
                            self._half_open = probe = True
                        self.active += 1
                        self.requests += 1
                        self._next_request = now + self.interval_s
                        break
                    self._cv.wait(min(1, max(.01, self._open_until - now, self._next_request - now)))
            finally:
                self._queue.remove(ticket)
        try:
            yield
        except BaseException as exc:
            self.report(False, failure_kind(exc), probe=probe)
            raise
        else:
            self.report(True, '', probe=probe)
        finally:
            with self._cv:
                self.active -= 1
                self._cv.notify_all()

    def report(self, ok, kind='', *, probe=False):
        with self._cv:
            now = self._clock()
            if probe:
                self._half_open = False
            if ok or kind == 'fatal':
                if probe:
                    self._open_until = self._outages = 0
                self._failures.clear()
                self._pressure = 0
                if self.adaptive and now - self._stable_since >= self.recovery_s:
                    self.limit = min(self.maximum, self.limit + 1)
                    self._stable_since = now
            else:
                self._stable_since = now
                self._failures.append(now)
                while self._failures and now - self._failures[0] > 30:
                    self._failures.popleft()
                if kind in ('429', 'timeout'):
                    self._pressure += 1
                    if self.adaptive and self._pressure >= 2:
                        self.limit = max(self.minimum, math.floor(self.limit * .7))
                        self._pressure = 0
                if probe or (not self._open_until and len(self._failures) >= self.breaker_failures):
                    self._open_until = now + min(self.max_cooldown_s, self.cooldown_s * 2 ** min(self._outages, 20))
                    self._outages += 1
                    self.breaker_count += 1
                    self._failures.clear()
            self._cv.notify_all()

    def note_retry(self):
        with self._cv:
            self.retries += 1

    def snapshot(self):
        with self._cv:
            return dict(name=self.name, active=self.active, limit=self.limit, maximum=self.maximum,
                        waiting=len(self._queue), state='half_open' if self._half_open else 'open' if self._open_until else 'closed',
                        retry_in_s=max(0, round(self._open_until - self._clock())),
                        breakers=self.breaker_count, retries=self.retries, requests=self.requests)

    def configure(self, maximum, minimum=1):
        with self._cv:
            old_maximum = self.maximum
            self.maximum, self.minimum = maximum, minimum
            self.limit = maximum if self.limit == old_maximum else min(maximum, max(minimum, self.limit))
            self._cv.notify_all()


class Runtime:
    def __init__(self, config):
        from carrel.runtime.retry import RetryPolicy
        self.policy = RetryPolicy(config.retry.max_attempts, config.retry.base_s, config.retry.cap_s)
        self.config = config
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self.pools = {}
        c = config.concurrency
        for name, size, interval in [('mineru', c.mineru, 0), ('download', c.download, 0),
                                     ('mineru:poll', 64, .125),
                                     ('download:arxiv', c.arxiv_download, c.arxiv_interval_s),
                                     ('openalex', 8, 1 / c.openalex_rps), ('sciverse', c.sciverse, 0)]:
            self.pool(name, size, interval_s=interval)

    def pool(self, name, maximum=12, **kwargs):
        with self._lock:
            if name not in self.pools:
                self.pools[name] = ResourcePool(name, maximum, breaker_failures=self.config.retry.breaker_failures,
                    max_cooldown_s=self.config.retry.breaker_max_cooldown_s, stop_event=self.stop_event, **kwargs)
            return self.pools[name]

    def snapshot(self):
        with self._lock:
            return [p.snapshot() for p in self.pools.values()]

    def check_stopped(self):
        if self.stop_event.is_set():
            raise RunInterrupted()
