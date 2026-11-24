"""Durable import batches shared by CLI and Web; library work has its own lane."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import re
import threading
import time
import uuid

from carrel import ids
from carrel.profile import parse_profile, default_profile, stored_profile
from carrel.pipeline import Pipeline
from carrel.runtime.context import priority, RunInterrupted
from carrel.runtime.limits import Runtime
from carrel.runtime.retry import classify, RetryableResponse
from carrel.store.paths import atomic_write_json, read_json, paper_dir

TERMINAL = frozenset(('succeeded', 'failed', 'partial', 'ready_with_warning'))


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class ImportScheduler:
    def __init__(self, config, db, *, workers=None, backend=None, pipeline=None,
                 retry_delays=(300, 900), finalizer=None):
        self.config, self.db = config, db
        self.workers = config.concurrency.papers if workers is None else workers
        if not 1 <= self.workers <= 64:
            raise ValueError('workers must be 1..64')
        self.runtime = Runtime(config)
        self.pipeline = pipeline or Pipeline(config, db, backend=backend, runtime=self.runtime)
        self.pipeline.defer_library = True
        self.retry_delays, self.finalizer = retry_delays, finalizer
        self._cv = threading.Condition(threading.RLock())
        self._batches = {}
        self._progress = {}
        self._sequence = 0
        self._threads = []
        self._library = []
        self._library_active = None
        self._stopping = False

    def _path(self, batch_id):
        if not re.fullmatch(r'batch-[a-f0-9]{12}', batch_id):
            raise ValueError('invalid batch id')
        return self.config.data_dir / '_batches' / (batch_id + '.json')

    def _save(self, batch):
        atomic_write_json(self._path(batch['id']), batch)

    def _start(self):
        if self._threads:
            return
        self._threads = [threading.Thread(target=self._run, name=f'carrel-paper-{i}', daemon=True) for i in range(self.workers)]
        self._threads.append(threading.Thread(target=self._run_library, name='carrel-library', daemon=True))
        for thread in self._threads:
            thread.start()

    def submit(self, inputs, *, force=False, profile=None):
        requested = parse_profile(profile) if profile is not None else default_profile(self.config.data_dir)
        entries = []
        for value in inputs:
            item = {'input': value} if isinstance(value, str) else dict(value)
            resolved = ids.resolve_input(item['input'])
            if resolved.kind == 'local_path' and Path(resolved.locator).is_file():
                digest = ids.sha256_file(Path(resolved.locator))
                existing = self.db.one('SELECT id FROM papers WHERE pdf_sha256=?', (digest,))
                resolved.paper_id = existing['id'] if existing else ids.paper_id_for(sha256=digest)
            entries.append(dict(input=item['input'], title=item.get('title', ''), paper_id=item.get('paper_id') or resolved.paper_id,
                                profile=parse_profile(item.get('profile', requested)).to_dict(),
                                job_id='job-' + uuid.uuid4().hex[:10], status='queued', stage='queued',
                                attempts=0, retries=0, next_at=0, error_code='', error_detail=''))
        if not entries:
            raise ValueError('batch requires inputs')
        with self._cv:
            if self._stopping:
                raise RuntimeError('scheduler is stopping')
            batch_id = 'batch-' + uuid.uuid4().hex[:12]
            batch = dict(id=batch_id, created_at=now(), started_at=time.time(), force=force, status='running',
                         items=entries, finalized=False, relinked=False, warning='')
            for entry in entries:
                entry['priority'] = self._sequence
                self._sequence += 1
            self._save(batch)
            self._batches[batch_id] = batch
            for entry in entries:
                self.db.create_job(entry['job_id'], entry['paper_id'], 'pipeline', now(), status='queued')
            self._start()
            self._cv.notify_all()
            return batch_id

    def resume(self, batch_id):
        with self._cv:
            if batch_id in self._batches:
                return batch_id
            batch = read_json(self._path(batch_id))
            if not isinstance(batch, dict) or batch.get('id') != batch_id:
                raise ValueError('batch manifest not found or invalid')
            self._batches[batch_id] = batch
            for entry in batch['items']:
                entry.setdefault('profile', parse_profile('full').to_dict())
                self._sequence = max(self._sequence, entry['priority'] + 1)
                if entry['status'] == 'running':
                    entry.update(status='queued', stage='queued')
                if entry['status'] not in TERMINAL:
                    if not self.db.one('SELECT id FROM jobs WHERE id=?', (entry['job_id'],)):
                        self.db.create_job(entry['job_id'], entry['paper_id'], 'pipeline', now(), status='queued')
                    self.db.execute("UPDATE jobs SET status='queued' WHERE id=?", (entry['job_id'],))
            if not batch['finalized']:
                batch['status'] = 'running'
            self._save(batch)
            self._start()
            self._maybe_finalize(batch)
            self._cv.notify_all()
            return batch_id

    def recover(self):
        restored = []
        known_jobs = set()
        for path in sorted((self.config.data_dir / '_batches').glob('batch-*.json')):
            batch = read_json(path, {})
            known_jobs.update(e['job_id'] for e in batch.get('items', []) if e.get('job_id'))
            if batch and not batch.get('finalized'):
                restored.append(self.resume(path.stem))
            elif batch and batch.get('id') == path.stem:
                with self._cv:
                    self._batches[path.stem] = batch
                    for entry in batch.get('items', []):
                        self._sequence = max(self._sequence, entry.get('priority', 0) + 1)
        # Older releases have no durable input manifests. Surface those jobs
        # as interrupted instead of leaving them permanently "running".
        for job in self.db.query("SELECT id FROM jobs WHERE stage='pipeline' AND status IN ('queued','running','retry_wait')"):
            if job['id'] not in known_jobs:
                self.db.finish_job(job['id'], 'failed', now(), 'interrupted',
                                   '上次处理已中断且没有可恢复的批次清单，请在论文页重试；已完成阶段会复用。')
        return restored

    def status(self, batch_id):
        with self._cv:
            batch = self._batches.get(batch_id) or read_json(self._path(batch_id))
            if not batch:
                raise ValueError('batch not found')
            return deepcopy(batch)

    def progress(self, paper_id):
        with self._cv:
            return deepcopy(self._progress.get(paper_id, {}))

    def snapshot(self):
        with self._cv:
            batches = deepcopy(list(self._batches.values()))
            stages = Counter(e['stage'] for b in batches for e in b['items'] if e['status'] == 'running')
            retries = [dict(batch_id=b['id'], paper_id=e['paper_id'], next_at=e['next_at'], error_code=e['error_code'])
                       for b in batches for e in b['items'] if e['status'] == 'retry_wait']
        from carrel.runtime.quota import MinerUQuota
        quota = MinerUQuota(self.config.data_dir, self.config.mineru.daily_pages_soft_limit, self.config.mineru.over_limit)
        visible = [b for b in batches if not b['finalized']] + [b for b in batches if b['finalized']][-30:]
        return dict(batches=visible, stages=dict(stages), resources=self.runtime.snapshot(), retries=retries,
                    mineru=quota.snapshot(), workers=self.workers)

    def wait(self, batch_id, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else float('inf')
        with self._cv:
            while not self._batches[batch_id]['finalized']:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('batch still running')
                self._cv.wait(min(.5, remaining))
            return deepcopy(self._batches[batch_id])

    def shutdown(self, *, wait=True):
        with self._cv:
            self._stopping = True
            self.runtime.stop_event.set()
            self._cv.notify_all()
        if wait:
            for thread in self._threads:
                thread.join()

    def _run(self):
        while True:
            with self._cv:
                while True:
                    if self._stopping:
                        return
                    eligible = [(e['priority'], b, e) for b in self._batches.values() for e in b['items']
                                if e['status'] in ('queued', 'retry_wait') and e['next_at'] <= time.time()]
                    if eligible:
                        _, batch, entry = min(eligible, key=lambda item: item[0])
                        entry.update(status='running', stage='parsing', attempts=entry['attempts'] + 1)
                        self._save(batch)
                        break
                    self._cv.wait(.5)
            self._execute(batch, entry)

    def _execute(self, batch, entry):
        token = priority.set(entry['priority'])
        self.db.execute("UPDATE jobs SET status='running', started_at=?, error_code='', error_detail='' WHERE id=?", (now(), entry['job_id']))
        def progress(p):
            with self._cv:
                entry['stage'] = p.stage
                entry['paper_id'] = p.paper_id
                self._progress[p.paper_id] = dict(stage=p.stage, detail=p.detail, at=now())
                self.db.execute('UPDATE jobs SET paper_id=? WHERE id=?', (p.paper_id, entry['job_id']))
        try:
            final_id = self.pipeline.process(entry['input'], force=batch['force'] and entry['attempts'] == 1,
                                             title=entry['title'], progress=progress, profile=entry['profile'])
            final_paper = self.db.get_paper(final_id) or {}
            final = final_paper.get('status', 'ready')
            if final == 'partial' and entry['retries'] < len(self.retry_delays):
                warning = read_json(paper_dir(self.config.data_dir, final_id) / 'translate_warnings.json', {})
                if warning.get('retryable'):
                    entry['paper_id'] = final_id
                    error = RetryableResponse('临时网络故障导致部分章节未完成，将复用已有译文继续补齐。')
                    error.code = 'translate_network_error'
                    raise error
            with self._cv:
                if final_paper.get('profile'):
                    entry['profile'] = stored_profile(final_paper).to_dict()
                entry.update(paper_id=final_id, status=final if final in ('partial', 'ready_with_warning') else 'succeeded',
                             stage=final, error_code='', error_detail='', finished_at=time.time())
                self._progress[final_id] = dict(stage=final, detail={}, at=now())
                self.db.execute('UPDATE jobs SET paper_id=? WHERE id=?', (final_id, entry['job_id']))
                self.db.finish_job(entry['job_id'], 'partial' if final == 'partial' else 'succeeded', now(),
                                   'processing_incomplete' if final == 'partial' else '',
                                   '所选处理内容尚未全部完成，可重试补齐。' if final == 'partial' else '')
        except RunInterrupted:
            with self._cv:
                entry.update(status='queued', stage='queued')
                self.db.execute("UPDATE jobs SET status='queued' WHERE id=?", (entry['job_id'],))
        except Exception as exc:
            with self._cv:
                entry.update(error_code=getattr(exc, 'code', 'internal_error'), error_detail=str(exc)[:1000])
                if classify(exc) == 'retry' and entry['retries'] < len(self.retry_delays):
                    entry.update(status='retry_wait', stage='retry_wait', next_at=time.time() + self.retry_delays[entry['retries']])
                    entry['retries'] += 1
                    entry['priority'] = self._sequence
                    self._sequence += 1
                    self.db.execute("UPDATE jobs SET status='retry_wait', error_code=?, error_detail=? WHERE id=?",
                                    (entry['error_code'], entry['error_detail'], entry['job_id']))
                else:
                    entry.update(status='failed', stage='failed', finished_at=time.time())
                    self.db.finish_job(entry['job_id'], 'failed', now(), entry['error_code'], entry['error_detail'])
        finally:
            priority.reset(token)
            with self._cv:
                self._save(batch)
                self._maybe_finalize(batch)
                self._cv.notify_all()

    def _maybe_finalize(self, batch):
        if not batch['finalized'] and batch['status'] != 'finalizing' and all(e['status'] in TERMINAL for e in batch['items']):
            batch['status'] = 'finalizing'
            self._save(batch)
            self._library.append(('batch', batch['id']))
            self._cv.notify_all()

    def submit_library(self, *, force=False, key=''):
        with self._cv:
            pending = self._library + ([self._library_active] if self._library_active else [])
            for kind, item in pending:
                if kind == 'tree' and item['key'] == key:
                    return dict(job_id=item['job_id'], paper_id='@library', duplicate=True, deduped=True)
            entry = dict(job_id='job-' + uuid.uuid4().hex[:10], force=force, key=key)
            self.db.create_job(entry['job_id'], '@library', 'motivation_tree', now(), status='queued')
            self._library.append(('tree', entry))
            self._start()
            self._cv.notify_all()
            return dict(job_id=entry['job_id'], paper_id='@library')

    def _run_library(self):
        while True:
            with self._cv:
                while not self._library and not self._stopping:
                    self._cv.wait(.5)
                if self._stopping:
                    return
                kind, item = self._library.pop(0)
                self._library_active = (kind, item)
            try:
                if kind == 'batch':
                    self._finalize(self._batches[item])
                else:
                    from carrel.motivation.tree import extend_tree, rebuild_tree, library_revision
                    self.db.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (now(), item['job_id']))
                    before = library_revision(self.config, self.db)
                    (rebuild_tree if item['force'] else extend_tree)(self.config, self.db, self.pipeline.backend)
                    if before != library_revision(self.config, self.db):
                        extend_tree(self.config, self.db, self.pipeline.backend)
                    self.db.finish_job(item['job_id'], 'succeeded', now())
            except RunInterrupted:
                return
            except Exception as exc:
                if kind == 'tree':
                    self.db.finish_job(item['job_id'], 'failed', now(), getattr(exc, 'code', 'internal_error'), str(exc)[:1000])
            finally:
                with self._cv:
                    self._library_active = None
                    self._cv.notify_all()

    def _finalize(self, batch):
        interrupted = False
        try:
            if self.finalizer:
                self.finalizer(batch)
            else:
                from carrel.relate.relink import relink_library
                from carrel.motivation.tree import extend_tree
                relink_library(self.db)
                with self._cv:
                    batch['relinked'] = True
                    self._save(batch)
                if any(e['status'] != 'failed' and parse_profile(e.get('profile', 'full')).motivation
                       for e in batch['items']):
                    extend_tree(self.config, self.db, self.pipeline.backend)
        except RunInterrupted:
            interrupted = True
            batch['status'] = 'running'
            raise
        except Exception as exc:
            batch['warning'] = f'库级整理未完成：{type(exc).__name__}: {str(exc)[:300]}'
        finally:
            with self._cv:
                if not interrupted:
                    batch.update(finalized=True, status='completed', finished_at=now(),
                             wall_s=round(time.time() - batch['started_at'], 2),
                             counts=dict(Counter(e['status'] for e in batch['items'])),
                             retries=sum(e['retries'] for e in batch['items']), resources=self.runtime.snapshot())
                self._save(batch)
                self._cv.notify_all()
