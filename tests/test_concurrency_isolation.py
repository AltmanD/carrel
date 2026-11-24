from concurrent.futures import ThreadPoolExecutor
import threading
import time

from carrel.models import DocModel
from carrel.parse.base import blocks_from_content_list, page_count
from carrel.store.paths import paper_dir, read_json
from tests.conftest import FakeBackend, FakeParser, sample_content_list


def test_fingerprints_and_partial_state_are_per_paper(pipeline_factory, tmp_path, db):
    gate = threading.Barrier(2)
    class Parser(FakeParser):
        def parse(self, path, **kwargs):
            content = sample_content_list()
            content[0]['text'] = path.read_bytes().decode()[-1] + ' unique paper'
            blocks = blocks_from_content_list(content)
            gate.wait(timeout=5)
            return DocModel(backend='fake', pages=page_count(blocks), blocks=blocks, quality=.9)
    class Backend(FakeBackend):
        def structured(self, **kwargs):
            time.sleep(.003)
            return super().structured(**kwargs)
    pipeline = pipeline_factory(parser=Parser(), backend=Backend())
    original = pipeline._stage_translate
    def translate(pid, meta, doc, progress, ctx):
        original(pid, meta, doc, progress, ctx)
        if doc.blocks[0].text.startswith('A'):
            ctx.partial_sections.append('injected missing section')
    pipeline._stage_translate = translate
    paths = [tmp_path / (key + '.pdf') for key in ('A', 'B')]
    for path in paths:
        path.write_bytes(b'%PDF-1.4 ' + path.stem.encode())
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda p: pipeline.process(str(p)), paths))
    assert [db.get_paper(pid)['status'] for pid in ids] == ['partial', 'ready']
    fingerprints = []
    for pid in ids:
        base = paper_dir(pipeline.data_dir, pid)
        doc = DocModel.from_dict(read_json(base / 'doc.json'))
        fingerprints.append(doc.fingerprint())
        for name in ('classification', 'assessment', 'motivation'):
            assert read_json(base / (name + '.json'))['doc_fingerprint'] == doc.fingerprint()
    assert len(set(fingerprints)) == 2


def test_six_papers_serial_and_parallel_have_identical_artifacts(tmp_path):
    from collections import Counter
    from carrel.config import AppConfig
    from carrel.pipeline import Pipeline
    from carrel.scheduler import ImportScheduler
    from carrel.store.db import Database
    from tests.conftest import FakeRelations
    results = []
    for workers in (1, 6):
        config = AppConfig(data_dir=tmp_path / str(workers))
        db = Database(config.data_dir / 'app.db')
        backend = FakeBackend()
        class Parser(FakeParser):
            def parse(self, path, **kwargs):
                content = sample_content_list()
                content[0]['text'] = kwargs['data_id'] + ' unique paper'
                blocks = blocks_from_content_list(content)
                time.sleep(.01)
                return DocModel(backend='fake', pages=page_count(blocks), blocks=blocks, quality=.9)
        scheduler = ImportScheduler(config, db, workers=workers, finalizer=lambda b: None)
        pipeline = Pipeline(config, db, parser=Parser(), backend=backend, relations=FakeRelations(), runtime=scheduler.runtime)
        pipeline._download = lambda url, path: path.write_bytes(b'%PDF-1.4 ' + url.encode())
        pipeline.defer_library = True
        scheduler.pipeline = pipeline
        try:
            batch = scheduler.wait(scheduler.submit([f'arxiv:2401.{i:05}' for i in range(6)]), timeout=15)
            assert batch['counts'] == {'succeeded': 6}
            artifacts = {}
            for row in db.list_papers():
                base = paper_dir(config.data_dir, row['id'])
                artifacts[row['id']] = {
                    'modules': {p.name: read_json(p) for p in (base / 'modules').glob('*.json')},
                    'translation': {p.name: read_json(p) for p in (base / 'translation').glob('*.json') if not p.name.startswith('_')},
                    'classification': read_json(base / 'classification.json'),
                    'summary': read_json(base / 'summary.json'),
                }
            results.append((artifacts, Counter(call['task'] for call in backend.calls)))
        finally:
            scheduler.shutdown()
            db.close()
    assert results[0] == results[1]
