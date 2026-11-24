from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from carrel.model.base import MessagesBackend, Capabilities
from carrel.runtime.quota import MinerUQuota
from carrel.store.paths import acquire_lock, release_lock, read_json


def test_probe_once_and_concurrent_cache_writes(tmp_path, monkeypatch):
    backend = MessagesBackend(base_url='https://fake.invalid', api_key='fake', model='a', caps_path=tmp_path / 'caps.json')
    calls = []
    def probe(*, model, thinking):
        calls.append(model)
        time.sleep(.01)
        return Capabilities(strict_schema=True), True
    monkeypatch.setattr(backend, '_probe', probe)
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda name: backend.capabilities(model=name), ['a']*5 + ['b']*3))
    assert all(c.strict_schema for c in answers)
    assert sorted(calls) == ['a', 'b']
    assert len(read_json(tmp_path / 'caps.json')) == 2


def test_probe_failure_not_cached_or_overwritten_by_another_model(tmp_path, monkeypatch):
    backend = MessagesBackend(base_url='https://fake.invalid', api_key='fake', model='a')
    barrier = threading.Barrier(2)
    def probe(*, model, thinking):
        barrier.wait(timeout=2)
        return (Capabilities(error='offline'), False) if model == 'a' else (Capabilities(strict_schema=True), True)
    monkeypatch.setattr(backend, '_probe', probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda model: backend.capabilities(model=model), ['a', 'b']))
    assert a.error == 'offline' and not b.error
    assert backend._caps_key('a', backend.thinking) not in backend._caps


def test_os_lock_releases_after_crash_and_rejects_second_holder(tmp_path):
    lock = acquire_lock(tmp_path)
    assert lock is not None
    code = "from pathlib import Path; from carrel.store.paths import acquire_lock; import sys; print(bool(acquire_lock(Path(sys.argv[1]))))"
    try:
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and result.stdout.strip() == 'False'
    finally:
        release_lock(lock)
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == 'True'
    # The child exited without release_lock; the kernel released its guard.
    lock = acquire_lock(tmp_path)
    assert lock is not None
    release_lock(lock)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process object lifetime')
def test_exited_process_with_open_handle_is_not_alive(tmp_path):
    from carrel.store.paths import _pid_alive

    process = subprocess.Popen([sys.executable, '-c', 'pass'])
    try:
        process.wait(timeout=10)
        # Popen still owns a native handle, so OpenProcess may succeed even
        # though the process has exited.
        path = tmp_path / 'server.lock'
        path.write_text(str(process.pid), encoding='utf-8')
        assert not _pid_alive(path)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_quota_counts_pages_atomically(tmp_path):
    from pypdf import PdfWriter
    source = tmp_path / 'paper.pdf'
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    writer.write(source)
    quota = MinerUQuota(tmp_path, limit=5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: quota.reserve(source), range(4)))
    assert quota.snapshot()['pages'] == 12
    assert quota.snapshot()['over_limit']


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows extended path aliases')
def test_lock_identity_normalizes_extended_path_prefix(tmp_path):
    from carrel.runtime.locks import path_key
    ordinary = tmp_path / 'not-yet-created.json'
    extended = Path('//?/' + str(ordinary.resolve()).replace(chr(92), '/'))
    assert path_key(ordinary) == path_key(extended)


def test_windows_atomic_replace_retries_without_truncating(tmp_path, monkeypatch):
    from carrel.store import paths
    target = tmp_path / 'manifest.json'
    paths.atomic_write_json(target, {'version': 1})
    original = paths.os.replace
    attempts = []
    def replace(source, dest):
        attempts.append(1)
        if len(attempts) < 3:
            assert paths.read_json(target) == {'version': 1}
            error = PermissionError('temporary sharing violation')
            error.winerror = 32
            raise error
        return original(source, dest)
    monkeypatch.setattr(paths.os, 'replace', replace)
    paths.atomic_write_json(target, {'version': 2})
    assert len(attempts) == 3 and paths.read_json(target) == {'version': 2}
    assert not list(tmp_path.glob('.tmp-*'))


def test_temporary_read_denial_does_not_reset_quota(tmp_path, monkeypatch):
    from carrel.store.paths import atomic_write_json
    quota = MinerUQuota(tmp_path)
    atomic_write_json(quota.path(), {'pages': 20, 'submissions': 1})
    original = Path.read_text
    attempts = []
    def read(path, **kwargs):
        if path == quota.path() and not attempts:
            attempts.append(1)
            error = PermissionError('temporary read sharing violation')
            error.winerror = 5
            raise error
        return original(path, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    source = tmp_path / 'unknown.pdf'
    source.write_bytes(b'%PDF-1.4')
    quota.reserve(source)
    assert quota.snapshot()['pages'] == 21


def test_startup_failure_releases_writer_lock(tmp_path, monkeypatch):
    from carrel import cli
    monkeypatch.setattr(cli, '_build', lambda args: (_ for _ in ()).throw(ValueError('bad config')))
    with pytest.raises(ValueError, match='bad config'):
        cli.main(['--data-dir', str(tmp_path), 'serve'])
    lock = acquire_lock(tmp_path)
    assert lock is not None
    release_lock(lock)


def test_configuration_loads_limits_and_live_provider_update(tmp_path):
    from carrel.config import load_config
    from carrel.model.router import ProviderRouter
    from carrel.providers import ProviderConfig
    from carrel.runtime.limits import Runtime
    source = tmp_path / 'local.toml'
    source.write_text('[concurrency]\npapers=4\nmodules_per_paper=2\n[retry]\nmax_attempts=3\n'
                      '[mineru]\npoll_interval=5\ntimeout_s=500\ndaily_pages_soft_limit=700\nover_limit="pause"\n')
    config = load_config(tmp_path, source)
    assert config.concurrency.papers == 4 and config.max_parallel_modules == 2
    assert config.retry.max_attempts == 3
    assert config.mineru.timeout_s == 500 and config.mineru.poll_interval == 5
    assert config.mineru.over_limit == 'pause' and config.mineru.daily_pages_soft_limit == 700
    config.providers['test'] = ProviderConfig(id='test', label='test', protocol='messages',
                                             base_url='https://example.invalid', model='test', max_concurrency=8)
    router = ProviderRouter.from_config(config)
    runtime = Runtime(config)
    router.configure_runtime(runtime)
    pool = runtime.pool('llm:test')
    config.providers['test'].max_concurrency = 3
    prepared = ProviderRouter.from_config(config)
    router.adopt(prepared)
    assert runtime.pool('llm:test') is pool and pool.limit == 3
