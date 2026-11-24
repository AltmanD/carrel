from carrel.relate.relink import relink_library
from carrel.store.db import Database


def test_completion_order_does_not_change_relations(tmp_path):
    results = []
    for order in [('a', 'b'), ('b', 'a')]:
        db = Database(tmp_path / (''.join(order) + '.db'))
        for key in order:
            pid = 'arxiv:' + key
            db.upsert_paper({'id': pid, 'title': key, 'status': 'ready'})
            rows = [{'src_id': pid, 'dst_id': 'external:b', 'kind': 'cites', 'dst_arxiv': 'b',
                     'dst_title': 'b', 'provider': 'openalex', 'coverage': 'complete', 'fetched_at': 'fixed'}] if key == 'a' else []
            db.replace_relations(pid, 'cites', rows)
            db.replace_relations(pid, 'cited_by', [])
            relink_library(db)
        relink_library(db)
        results.append([dict(r) for r in db.query('SELECT * FROM relations ORDER BY src_id,kind,dst_id')])
        db.close()
    assert results[0] == results[1]
    assert {(r['src_id'], r['dst_id'], r['kind']) for r in results[0]} == {
        ('arxiv:a', 'arxiv:b', 'cites'), ('arxiv:b', 'arxiv:a', 'cited_by')}
