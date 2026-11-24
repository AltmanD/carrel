"""Fault injection at real adapter boundaries; no external network calls."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import zipfile

import httpx
import pytest

from carrel.model.base import MessagesBackend, ModelAuthError
from carrel.parse.mineru_api import MinerUApiParser
from carrel.pipeline import PipelineError
from carrel.runtime.limits import ResourcePool
from carrel.runtime.retry import RetryPolicy


def response(status=200, content=b'', headers=None):
    return httpx.Response(status, content=content, headers=headers, request=httpx.Request('GET', 'https://fake.invalid'))


def test_download_recovers_html_network_and_503_then_atomic_pdf(pipeline_factory, tmp_path, monkeypatch):
    pipeline = pipeline_factory()
    from carrel.pipeline import Pipeline
    pipeline._download = Pipeline._download.__get__(pipeline)
    pipeline.runtime.policy = RetryPolicy(4, 0, 0)
    script = [response(200, b'<html>rate limited</html>'), httpx.ConnectError('reset'), response(503), response(200, b'%PDF-1.4 good')]
    @contextmanager
    def stream(*args, **kwargs):
        next_response = script.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        yield next_response
    monkeypatch.setattr('httpx.stream', stream)
    dest = tmp_path / 'source.pdf'
    pipeline._download('https://fake.invalid/paper.pdf', dest)
    assert dest.read_bytes() == b'%PDF-1.4 good' and not dest.with_suffix('.part').exists()
    assert pipeline.runtime.pool('download').active == 0


def test_arxiv_switches_only_after_transient_exhaustion(pipeline_factory, tmp_path, monkeypatch):
    from carrel.pipeline import Pipeline
    pipeline = pipeline_factory()
    pipeline._download = Pipeline._download.__get__(pipeline)
    pipeline.runtime.policy = RetryPolicy(2, 0, 0)
    pipeline.runtime.pool('download:arxiv').interval_s = 0
    urls = []
    status = [503]
    @contextmanager
    def stream(method, url, **kwargs):
        urls.append(url)
        yield response(200, b'%PDF-1.4 fallback') if 'export.' in url else response(status[0])
    monkeypatch.setattr('httpx.stream', stream)
    pipeline._download('https://arxiv.org/pdf/2401.00001', tmp_path / 'a.pdf')
    assert len(urls) == 3 and 'export.' in urls[-1]
    urls.clear(); status[0] = 404
    with pytest.raises(PipelineError) as caught:
        pipeline._download('https://arxiv.org/pdf/2401.00002', tmp_path / 'b.pdf')
    assert len(urls) == 1 and caught.value.retryable is False
    assert not (tmp_path / 'b.part').exists()


def test_llm_http_date_retry_and_auth_failure(monkeypatch):
    backend = MessagesBackend(base_url='https://fake.invalid', api_key='test', model='fake')
    backend.resource = ResourcePool('llm', 1)
    backend.retry_policy = RetryPolicy(4, 0, 0)
    waits = []
    backend.resource.stop_event.wait = lambda delay: waits.append(delay) or False
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=4))
    script = [response(429, headers={'Retry-After': date}), response(503), httpx.ReadTimeout('timeout'), response(200, b'{}')]
    def post(*args, **kwargs):
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr('httpx.post', post)
    assert '_duration_ms' in backend._post('/v1/messages', {})
    assert 2 <= waits[0] <= 4 and backend.resource.active == 0
    script.append(response(401))
    with pytest.raises(ModelAuthError) as caught:
        backend._post('/v1/messages', {})
    assert not caught.value.retryable


def test_mineru_retries_upload_zip_and_poll_without_upload_slots(tmp_path, monkeypatch):
    parser = MinerUApiParser(token='fake', poll_interval=0, timeout_s=3)
    parser.policy = RetryPolicy(4, 0, 0)
    parser.resource = ResourcePool('mineru', 1)
    upload = {'code': 0, 'data': {'batch_id': 'batch', 'file_urls': ['https://upload.invalid']}}
    script = [response(503), response(200, json.dumps(upload).encode())]
    monkeypatch.setattr('httpx.request', lambda *a, **kw: script.pop(0))
    assert parser._request_upload_slot('a.pdf', 'a') == ('batch', 'https://upload.invalid')
    source = tmp_path / 'a.pdf'; source.write_bytes(b'%PDF-1.4')
    uploads = [httpx.ConnectError('reset'), response(200)]
    def put(*args, **kw):
        item = uploads.pop(0)
        if isinstance(item, Exception):
            raise item
        assert 'headers' not in kw
        return item
    monkeypatch.setattr('httpx.put', put)
    parser._upload('https://upload.invalid', source)
    done = {'code': 0, 'data': {'extract_result': [{'state': 'done', 'full_zip_url': 'https://zip.invalid'}]}}
    script[:] = [response(503), response(200, json.dumps(done).encode())]
    with parser.resource.slot():
        assert parser._poll('batch', progress=None)['state'] == 'done'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('content_list.json', json.dumps([{'type': 'text', 'text': 'hello'}]))
    zips = [response(200, b'HTML'), response(200, buffer.getvalue())]
    monkeypatch.setattr('httpx.get', lambda *a, **kw: zips.pop(0))
    assert parser._fetch_content_list('https://zip.invalid', tmp_path / 'raw')[0]['text'] == 'hello'
    assert parser.resource.active == 0


def test_mineru_terminal_failure_drops_checkpoint_but_timeout_keeps_it(tmp_path, monkeypatch):
    from carrel.parse.base import ParseRemoteFailed
    from carrel.store.paths import read_json
    source = tmp_path / 'a.pdf'
    source.write_bytes(b'%PDF-1.4 test')
    parser = MinerUApiParser(token='fake', poll_interval=0, timeout_s=1)
    monkeypatch.setattr(parser, '_request_upload_slot', lambda *args: ('remote-batch', 'https://upload.invalid'))
    monkeypatch.setattr(parser, '_upload', lambda *args: None)
    terminal = [False]
    def poll(*args, **kwargs):
        error = ParseRemoteFailed('busy')
        error.retryable = True
        error.remote_terminal = terminal[0]
        raise error
    monkeypatch.setattr(parser, '_poll', poll)
    target = tmp_path / 'raw'
    with pytest.raises(ParseRemoteFailed):
        parser.parse(source, out_dir=target)
    assert read_json(target / 'upload.json')['batch_id'] == 'remote-batch'
    terminal[0] = True
    with pytest.raises(ParseRemoteFailed):
        parser.parse(source, out_dir=target)
    assert not (target / 'upload.json').exists()
