"""Graph invalidation covers committed edits without scanning artifacts."""
import pytest

from carrel.graph.revision import revision
from carrel.store.paths import atomic_write_json, paper_dir


def test_revision_covers_raw_transaction_and_equal_size_changes(db):
    db.upsert_paper({'id': 'a', 'title': 'Alpha'})
    db.upsert_paper({'id': 'b', 'title': 'Beta'})
    start = db.graph_revision
    db.replace_relations('a', 'cites', [{'dst_id': 'b', 'provider': 'one'}])
    assert db.graph_revision > start
    start = db.graph_revision
    with db.transaction() as conn:
        conn.execute("UPDATE relations SET provider='two' WHERE src_id='a'")
    assert db.graph_revision > start
    start = db.graph_revision
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("UPDATE papers SET title='Changed' WHERE id='a'")
        raise RuntimeError('rollback')
    assert db.graph_revision == start


def test_artifact_changes_invalidate_but_progress_does_not(tmp_path):
    base = paper_dir(tmp_path, 'a')
    start = revision(tmp_path)
    atomic_write_json(base / 'classification.json', {'categories': []})
    assert revision(tmp_path) == start + 1
    atomic_write_json(base / 'progress_meta.json', {'percent': 80})
    assert revision(tmp_path) == start + 1
    atomic_write_json(tmp_path / 'motivation_tree.json', {'edges': []})
    assert revision(tmp_path) == start + 2
    atomic_write_json(base / 'modules' / 'introduction.json', {'summary': 'finished'})
    assert revision(tmp_path) == start + 3
    atomic_write_json(base / 'translation' / 'introduction.json', {'blocks': []})
    assert revision(tmp_path) == start + 4
    atomic_write_json(tmp_path / 'graph_display.json', {'branches': {}})
    assert revision(tmp_path) == start + 5
