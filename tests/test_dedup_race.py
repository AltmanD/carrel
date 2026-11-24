from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from carrel.store.db import Database
from tests.conftest import FakeParser


def test_same_bytes_are_parsed_once(pipeline_factory, tmp_path, db):
    class Parser(FakeParser):
        def parse(self, *args, **kwargs):
            time.sleep(.05)
            return super().parse(*args, **kwargs)
    parser = Parser()
    pipeline = pipeline_factory(parser=parser)
    paths = [tmp_path / (name + '.pdf') for name in ('a', 'b')]
    for path in paths:
        path.write_bytes(b'%PDF-1.4 same bytes')
    barrier = threading.Barrier(2)
    def run(path):
        barrier.wait(timeout=3)
        return pipeline.process(str(path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(run, paths))
    assert len(set(result)) == 1 and parser.calls == 1
    assert len(db.list_papers()) == 1
    assert len(list((pipeline.data_dir / 'papers').iterdir())) == 1


def test_url_and_local_copy_share_one_record(pipeline_factory, tmp_path, db, monkeypatch):
    parser = FakeParser()
    pipeline = pipeline_factory(parser=parser)
    content = b'%PDF-1.4 shared content'
    source = tmp_path / 'local.pdf'; source.write_bytes(content)
    monkeypatch.setattr(pipeline, '_download', lambda url, dest: dest.write_bytes(content))
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(pipeline.process, ['arxiv:2401.00001', str(source)]))
    assert len(set(result)) == 1 and parser.calls == 1 and len(db.list_papers()) == 1


def test_unique_sha_and_duplicate_migration_are_visible(tmp_path):
    path = tmp_path / 'app.db'
    db = Database(path)
    db.upsert_paper({'id': 'a', 'pdf_sha256': 'same'})
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_paper({'id': 'b', 'pdf_sha256': 'same'})
    db.execute('DROP INDEX idx_papers_sha')
    db.upsert_paper({'id': 'b', 'pdf_sha256': 'same'})
    db.execute("UPDATE schema_meta SET value='5' WHERE key='version'")
    db.close()
    with pytest.raises(ValueError, match='a,b'):
        Database(path)
    assert list((tmp_path / '_backup').glob('schema-v5-*.db'))
