import time

from carrel.graph.service import GraphService
from carrel.store.paths import atomic_write_json, paper_dir


def ready(service, **kwargs):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = service.explore(**kwargs)
        if result['status'] == 'error':
            raise AssertionError(result)
        if result['status'] == 'ready' and not result['rebuilding']:
            return result
        time.sleep(.01)
    raise AssertionError('projection timeout')


def test_overview_all_papers_hot_path_and_revision(db, tmp_path, monkeypatch):
    db.upsert_paper({'id': 'a', 'title': 'A', 'status': 'indexed'})
    service = GraphService(db, tmp_path)
    try:
        monkeypatch.setattr(service, 'motivation_tree', lambda: (_ for _ in ()).throw(AssertionError('inactive view')))
        result = ready(service)
        assert result['total'] == 1
        assert result['groups'][0]['coverage']['indexed'] == 1
        assert 'graph' not in result
        original = service._read_json_cached
        monkeypatch.setattr(service, '_read_json_cached', lambda path: (_ for _ in ()).throw(AssertionError('hot read')))
        assert service.explore()['revision'] == result['revision']
        assert service.explore(mode='group')['graph']['edges'] == []
        monkeypatch.setattr(service, '_read_json_cached', original)
        db.upsert_paper({'id': 'b', 'title': 'B', 'status': 'indexed'})
        assert ready(service)['total'] == 2
    finally:
        service.close()


def test_directed_neighbors_and_manual_label(db, tmp_path):
    for pid in ('a', 'b', 'c'):
        db.upsert_paper({'id': pid, 'title': pid, 'status': 'indexed'})
    db.replace_relations('a', 'cites', [{'dst_id': 'b'}])
    db.replace_relations('b', 'cited_by', [{'dst_id': 'a'}])
    db.replace_relations('b', 'cites', [{'dst_id': 'a'}])
    db.replace_relations('c', 'cited_by', [{'dst_id': 'a'}])
    atomic_write_json(paper_dir(tmp_path, 'a') / 'graph_display.json', {'label': 'Manual'})
    service = GraphService(db, tmp_path)
    try:
        result = ready(service, mode='neighbors', focus='a', limit=1)
        assert result['neighbor_total'] == 2
        assert len(result['graph']['nodes']) == 2
        assert result['graph']['focus'] == next(n['id'] for n in result['graph']['nodes'] if n['focus'])
        assert len(result['graph']['edges']) == 2
        detail = service.node_detail('a')
        assert detail['counts'] == {'cites': 2, 'cited_by': 1, 'similar': 0}
        assert detail['node']['label'] == 'Manual'
    finally:
        service.close()


def test_collection_intersection_and_keywords(db, tmp_path):
    for pid in ('a', 'b', 'c'):
        db.upsert_paper({'id': pid, 'title': pid, 'status': 'indexed'})
    with db.transaction() as conn:
        conn.execute("INSERT INTO collections(id,name,sort,created_at) VALUES('one','First',0,'now')")
        conn.execute("INSERT INTO collections(id,name,sort,created_at) VALUES('two','Second',1,'now')")
        conn.executemany('INSERT INTO paper_collections(paper_id,collection_id,rank) VALUES(?,?,?)', [('a','one',2),('b','one',1),('a','two',0)])
        conn.executemany('INSERT INTO tags(paper_id,tag) VALUES(?,?)', [('a','x'),('a','y'),('b','x')])
    service = GraphService(db, tmp_path)
    try:
        result = ready(service, group_by='collection')
        assert result['total'] == 3
        assert sum(g['count'] for g in result['groups']) == 4
        result = service.explore(collection=['one'], keyword=['x','y'], keywords_match='all')
        assert result['total'] == 1
        result = service.explore(mode='group', group_by='collection', group_id='one')
        assert [n['meta']['paper_id'] for n in result['graph']['nodes']] == ['b','a']
        result = service.explore(mode='group', group_by='collection', collection=['two'])
        assert result['graph']['nodes'][0]['meta']['branch']['key'] == 'two'
    finally:
        service.close()


def test_tree_boundaries_keep_original_predecessor(db, tmp_path, monkeypatch):
    from carrel.graph.build import _paper_node
    from carrel.graph.model import Graph, Edge
    for pid in ('a', 'b', 'c'):
        db.upsert_paper({'id': pid, 'title': pid, 'status': 'indexed'})
    with db.transaction() as conn:
        conn.executemany('INSERT INTO tags(paper_id,tag) VALUES(?,?)', [('b','inside'),('c','inside')])
    nodes = [_paper_node(db.get_paper(pid), '') for pid in ('a','b','c')]
    tree = Graph(scope='motivation_tree', nodes=nodes, edges=[Edge(nodes[0].id,nodes[1].id,'motivation'), Edge(nodes[1].id,nodes[2].id,'motivation')])
    service = GraphService(db, tmp_path)
    monkeypatch.setattr(service, 'motivation_tree', lambda: tree)
    try:
        result = ready(service, view='tree', keyword=['inside'])
        key = result['groups'][0]['id']
        result = service.explore(view='tree', mode='branch', group_id=key, keyword=['inside'], limit=1, include_context=True)
        assert result['boundary'][0]['predecessor_id'] == 'a'
        assert result['shown'] == 1
        assert len(result['graph']['nodes']) == 2
        assert len(result['graph']['edges']) == 1
        assert result['collapsed'][0]['count'] == 1
    finally:
        service.close()

def test_failed_refresh_retains_snapshot_and_explicit_retry(db, tmp_path, monkeypatch):
    db.upsert_paper({'id': 'a', 'title': 'A', 'status': 'indexed'})
    service = GraphService(db, tmp_path)
    try:
        ready(service)
        project = service._explorer.project
        monkeypatch.setattr(service._explorer, 'project', lambda token: (_ for _ in ()).throw(ValueError('broken snapshot')))
        service.refresh()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = service.explore()
            if result.get('refresh_error'):
                break
            time.sleep(.01)
        assert result['total'] == 1 and result['stale']
        assert result['refresh_error'] == 'broken snapshot'
        monkeypatch.setattr(service._explorer, 'project', project)
        service.refresh()
        assert not ready(service)['stale']
    finally:
        service.close()

def test_empty_collection_bounded_edges_and_tasks(db, tmp_path):
    for pid in ('a', 'b', 'c'):
        db.upsert_paper({'id': pid, 'title': pid})
    db.replace_relations('a', 'cites', [{'dst_id': 'b'}, {'dst_id': 'c'}])
    with db.transaction() as conn:
        conn.execute("INSERT INTO collections(id,name,sort,created_at) VALUES('empty','Empty',0,'now')")
        conn.execute("INSERT INTO jobs(id,paper_id,stage,status) VALUES('j','a','parse','failed')")
    service = GraphService(db, tmp_path)
    try:
        result = ready(service, group_by='collection')
        assert next(g for g in result['groups'] if g['id'] == 'empty')['count'] == 0
        assert result['stats']['tasks'] == {'pending': 0, 'failed': 1}
        result = service.explore(mode='group', include_cites=True, edge_limit=1)
        assert len(result['graph']['edges']) == 1
        assert result['edge_omitted'] == 1
        result = service.explore(mode='group', include_cites=True, limit=1)
        assert not result['graph']['edges']
    finally:
        service.close()


def test_projection_restart_reuses_coverage(db, tmp_path, monkeypatch):
    import carrel.graph.explorer as explorer
    db.upsert_paper({'id': 'a', 'title': 'A'})
    first = GraphService(db, tmp_path)
    ready(first)
    first.close()
    monkeypatch.setattr(explorer, 'paper_coverage', lambda _: (_ for _ in ()).throw(AssertionError('unchanged paper rebuilt')))
    second = GraphService(db, tmp_path)
    try:
        assert second._explorer.snapshots['graph']['token'] is None
        assert ready(second)['total'] == 1
        db.replace_relations('a', 'cites', [])
        assert ready(second)['total'] == 1
    finally:
        second.close()


def test_category_projection_primary_and_dedup(db, tmp_path):
    db.upsert_paper({'id': 'a', 'title': 'A'})
    atomic_write_json(paper_dir(tmp_path, 'a') / 'classification.json', {
        'primary_category_id': 'chosen',
        'categories': [
            {'discipline': 'D', 'field': 'F', 'category_id': 'other'},
            {'discipline': 'D', 'field': 'G', 'category_id': 'chosen'},
            {'discipline': 'D', 'field': 'G', 'category_id': 'chosen'}]})
    service = GraphService(db, tmp_path)
    try:
        categories = service.categories_for({'id': 'a'})
        assert len(categories) == 2
        assert categories[0]['category_id'] == 'chosen' and categories[0]['primary']
        assert all(group['count'] == 1 for group in ready(service)['groups'])
        facets = service.explore()['facets']['categories']
        assert next(c for c in facets if c['id'] == 'D')['count'] == 1
        assert next(c for c in facets if c['id'] == 'D')['level'] == 1
        assert next(c for c in facets if c['id'] == 'chosen')['level'] == 2
        assert service.explore(category=['D'])['total'] == 1
    finally:
        service.close()


def test_tree_pagination_ancestors_and_stable_name(db, tmp_path, monkeypatch):
    from carrel.graph.build import _paper_node
    from carrel.graph.model import Graph, Edge
    for pid in ('a', 'b', 'c'):
        db.upsert_paper({'id': pid, 'title': pid})
    with db.transaction() as conn:
        conn.execute("INSERT INTO tags(paper_id,tag) VALUES('c','last')")
    nodes = [_paper_node(db.get_paper(pid), '') for pid in ('a', 'b', 'c')]
    tree = Graph(scope='motivation_tree', nodes=nodes, edges=[
        Edge(nodes[0].id, nodes[1].id, 'motivation'),
        Edge(nodes[1].id, nodes[2].id, 'motivation'),
        Edge(nodes[0].id, nodes[2].id, 'motivation', evidence='Supporting text', primary=False)])
    service = GraphService(db, tmp_path)
    monkeypatch.setattr(service, 'motivation_tree', lambda: tree)
    try:
        all_groups = ready(service, view='tree')['groups']
        assert all_groups[0]['parent'] == '未分类'
        result = service.explore(view='tree', mode='branch', focus='c', limit=2)
        assert [n['meta']['paper_id'] for n in result['graph']['nodes']] == ['b', 'c']
        assert len(result['graph']['edges']) == 1
        result = service.explore(view='tree', mode='branch', q='c', limit=2)
        assert [n['meta']['paper_id'] for n in result['graph']['nodes']] == ['b', 'c']
        atomic_write_json(tmp_path / 'graph_display.json', {'nodes': {'a': 'Keep'}})
        service.save_branch_name(all_groups[0]['id'], 'Custom mainline')
        renamed = ready(service, view='tree')
        assert renamed['groups'][0]['label'] == 'Custom mainline'
        assert renamed['revision'] != result['revision']
        from carrel.store.paths import read_json
        assert read_json(tmp_path / 'graph_display.json')['nodes'] == {'a': 'Keep'}
        service.save_branch_name(all_groups[0]['id'], '')
        assert ready(service, view='tree')['groups'][0]['label'] == all_groups[0]['label']
        filtered = service.explore(view='tree', keyword=['last'])['groups']
        assert all_groups[0]['label'] == filtered[0]['label']
        result = service.explore(view='tree', mode='branch', offset=2, limit=1, include_context=True)
        assert result['boundary'][0]['reason'] == 'pagination'
        assert len(result['graph']['nodes']) == 2
        assert len(result['graph']['edges']) == 1
        ready(service)
        details = service.node_detail('c')
        assert any(not edge['primary'] and edge['evidence'] == 'Supporting text' for edge in details['motivation_edges'])
    finally:
        service.close()
