from collections import Counter
import threading
import time

import pytest
from fastapi.testclient import TestClient

from carrel.pipeline import PipelineError
from carrel.scheduler import ImportScheduler
from carrel.store.paths import atomic_write_json, read_json


class Pipeline:
    def __init__(self, db):
        self.db = db
        self.calls = Counter()

    def process(self, raw, **kwargs):
        self.calls[raw] += 1
        if raw.endswith('00001'):
            error = PipelineError('download_failed', 'transient')
            error.retryable = True
            raise error
        if raw.endswith('00002'):
            error = PipelineError('download_failed', 'HTTP 404')
            error.retryable = False
            raise error
        self.db.upsert_paper({'id': raw, 'status': 'ready'})
        return raw


def test_retry_ceiling_fatal_and_one_finalization(config, db):
    pipeline = Pipeline(db)
    finalized = []
    scheduler = ImportScheduler(config, db, pipeline=pipeline, workers=2, retry_delays=(.01, .02), finalizer=lambda b: finalized.append(b['id']))
    batch_id = scheduler.submit(['arxiv:2401.00001', 'arxiv:2401.00002', 'arxiv:2401.00003'])
    try:
        batch = scheduler.wait(batch_id, timeout=5)
        assert list(pipeline.calls.values()) == [3, 1, 1]
        assert batch['counts'] == {'failed': 2, 'succeeded': 1}
        assert batch['retries'] == 2 and finalized == [batch_id]
    finally:
        scheduler.shutdown()


def test_resume_only_unfinished_entries_and_saved_finalization(config, db, monkeypatch):
    first = ImportScheduler(config, db, pipeline=Pipeline(db), finalizer=lambda b: None)
    monkeypatch.setattr(first, '_start', lambda: None)
    batch_id = first.submit(['arxiv:2401.00003', 'arxiv:2401.00004'])
    manifest = first.status(batch_id)
    manifest['items'][0]['status'] = 'succeeded'
    manifest['items'][1]['status'] = 'running'
    manifest['items'][1]['attempts'] = 1
    atomic_write_json(first._path(batch_id), manifest)
    second = ImportScheduler(config, db, pipeline=Pipeline(db), finalizer=lambda b: None)
    try:
        assert second.recover() == [batch_id]
        batch = second.wait(batch_id, timeout=5)
        assert second.pipeline.calls == {'arxiv:2401.00004': 1}
        assert batch['finalized'] and batch['items'][1]['attempts'] == 2
    finally:
        second.shutdown()
    third = ImportScheduler(config, db, pipeline=Pipeline(db), finalizer=lambda b: pytest.fail('must not finalize twice'))
    try:
        third.resume(batch_id)
        assert third.wait(batch_id, timeout=2)['finalized']
        assert not third.pipeline.calls
    finally:
        third.shutdown()


def test_delayed_retry_does_not_hold_paper_worker(config, db):
    pipeline = Pipeline(db)
    scheduler = ImportScheduler(config, db, workers=1, pipeline=pipeline, retry_delays=(10,), finalizer=lambda b: None)
    try:
        batch_id = scheduler.submit(['arxiv:2401.00001', 'arxiv:2401.00003'])
        deadline = time.monotonic() + 2
        while pipeline.calls['arxiv:2401.00003'] == 0 and time.monotonic() < deadline:
            time.sleep(.01)
        assert pipeline.calls['arxiv:2401.00003'] == 1
        assert scheduler.status(batch_id)['items'][0]['status'] == 'retry_wait'
    finally:
        scheduler.shutdown()


def test_web_fifty_links_form_one_batch(config, db, monkeypatch):
    from carrel.web.app import create_app
    app = create_app(config, db)
    scheduler = app.state.jobs.scheduler
    monkeypatch.setattr(scheduler, '_start', lambda: None)
    with TestClient(app) as client:
        response = client.post('/api/import/batch', headers={'X-Carrel': '1'}, json={'inputs': [
            {'input': f'arxiv:2401.{i:05}', 'title': f'Paper {i}'} for i in range(50)]})
        assert response.status_code == 200
        data = response.json()
        assert len(data['jobs']) == 50
        overview = client.get('/api/batches').json()
        assert len(overview['batches']) == 1 and len(overview['batches'][0]['items']) == 50
        assert overview['workers'] == 6 and overview['mineru']['pages'] == 0
        assert 'data-batch-dashboard' in client.get('/jobs').text
        invalid = client.post('/api/import/batch', headers={'X-Carrel': '1'}, json={'inputs': [
            {'input': 'arxiv:2401.00001'}, {'input': 'invalid'}]})
        assert invalid.status_code == 400
        assert len(db.list_jobs(100)) == 50


def test_cli_directory_dry_run_and_writer_lock(tmp_path, monkeypatch, capsys):
    from carrel.cli import main
    from carrel.store.paths import acquire_lock, release_lock
    source, library = tmp_path / 'source', tmp_path / 'kb'
    source.mkdir(); (source / 'a.pdf').write_bytes(b'%PDF-1.4 sample')
    assert main(['--data-dir', str(library), 'batch', str(source), '--dry-run']) == 0
    assert not library.exists()
    assert 'a.pdf' in capsys.readouterr().out
    lock = acquire_lock(library)
    try:
        assert main(['--data-dir', str(library), 'import', str(source / 'a.pdf')]) == 1
        assert not (library / 'app.db').exists()
    finally:
        release_lock(lock)


def test_transport_partial_retries_but_quality_partial_stays_visible(config, db):
    from carrel.store.paths import paper_dir
    class PartialPipeline(Pipeline):
        def process(self, raw, **kwargs):
            self.calls[raw] += 1
            self.db.upsert_paper({'id': raw, 'status': 'partial'})
            atomic_write_json(paper_dir(config.data_dir, raw) / 'translate_warnings.json',
                              {'retryable': raw.endswith('00003')})
            return raw
    pipeline = PartialPipeline(db)
    scheduler = ImportScheduler(config, db, pipeline=pipeline, retry_delays=(.01, .01), finalizer=lambda b: None)
    try:
        batch = scheduler.wait(scheduler.submit(['arxiv:2401.00003', 'arxiv:2401.00004']), timeout=5)
        assert batch['counts'] == {'partial': 2}
        assert pipeline.calls == {'arxiv:2401.00003': 3, 'arxiv:2401.00004': 1}
        assert batch['retries'] == 2
    finally:
        scheduler.shutdown()


def test_legacy_job_without_manifest_is_visibly_interrupted(config, db):
    db.create_job('job-old', 'arxiv:2401.00003', 'pipeline', 'old', status='running')
    scheduler = ImportScheduler(config, db, pipeline=Pipeline(db), finalizer=lambda b: None)
    assert scheduler.recover() == []
    job = db.one("SELECT * FROM jobs WHERE id='job-old'")
    assert job['status'] == 'failed' and job['error_code'] == 'interrupted'
