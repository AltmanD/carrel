"""Navigation shell and read-only graph API contract, without model calls."""
from fastapi.testclient import TestClient

from carrel.web.app import create_app


class ExplorerStub:
    def __init__(self):
        self.calls = []
        self.refreshes = 0
        self.state = 'ready'

    def library_graph(self, **kwargs):
        raise AssertionError('page must not build the library graph')

    def motivation_tree(self, *args):
        raise AssertionError('page must not build the inactive graph')

    def explore(self, **kwargs):
        self.calls.append(kwargs)
        return {'status': self.state, 'revision': 'r1', 'groups': [], 'stats': {}, 'filters': kwargs}

    def refresh(self):
        self.refreshes += 1

    def invalidate(self):
        self.refreshes += 1

    def close(self):
        pass


def client_with_stub(config, db):
    app = create_app(config, db)
    original = app.state.graphs
    if hasattr(original, 'close'):
        original.close()
    app.state.graphs = ExplorerStub()
    return TestClient(app), app.state.graphs


def test_page_is_shell_without_full_graph_or_elk(config, db):
    client, service = client_with_stub(config, db)
    for view in ('graph', 'tree'):
        response = client.get('/relations', params={'view': view})
        assert response.status_code == 200
        assert 'data-graph-explorer' in response.text
        assert 'id="graph-data"' not in response.text
        assert 'src="/static/vendor/elk.bundled.js' not in response.text
    assert service.calls == []


def test_explorer_repeated_facets_and_conditional_response(config, db):
    client, service = client_with_stub(config, db)
    url = '/api/graph/explore?collection=a&collection=b&collection=a&keyword=RL&mode=group'
    first = client.get(url)
    assert first.status_code == 200
    assert service.calls[-1]['collection'] == ['a', 'b']
    assert service.calls[-1]['keyword'] == ['RL']
    assert service.calls[-1]['limit'] == 24
    assert client.get(url, headers={'If-None-Match': first.headers['etag']}).status_code == 304
    changed = client.get(url + '&sort=name', headers={'If-None-Match': first.headers['etag']})
    assert changed.status_code == 200
    service.state = 'building'
    pending = client.get(url)
    assert pending.status_code == 202 and pending.headers['cache-control'] == 'no-store'


def test_invalid_parameters_do_not_start_build(config, db):
    client, service = client_with_stub(config, db)
    for query in ('limit=0', 'limit=201', 'offset=-1', 'view=no', 'mode=neighbors',
                  'group_by=no', 'keywords_match=no', 'sort=no'):
        assert client.get('/api/graph/explore?' + query).status_code == 422
    assert service.calls == []


def test_refresh_requires_local_request_header(config, db):
    client, service = client_with_stub(config, db)
    assert client.post('/api/graph/refresh').status_code == 403
    assert service.refreshes == 0
    assert client.post('/api/graph/refresh', headers={'X-Carrel': '1'}).status_code == 202
    assert service.refreshes == 1


def test_manual_method_name_preserves_paper_title(config, db):
    from carrel.graph.display import display_name
    db.upsert_paper({'id': 'method-test', 'title': 'Original complete paper title', 'status': 'indexed'})
    client, service = client_with_stub(config, db)
    endpoint = '/api/graph/node-label'
    payload = {'paper_id': 'method-test', 'label': 'Method-X'}
    assert client.post(endpoint, json=payload).status_code == 403
    response = client.post(endpoint, json=payload, headers={'X-Carrel': '1'})
    assert response.status_code == 200
    assert display_name(config.data_dir, 'method-test') == 'Method-X'
    assert db.get_paper('method-test')['title'] == 'Original complete paper title'
    assert service.refreshes == 1
    assert client.post(endpoint, json={**payload, 'label': 'x' * 61},
                       headers={'X-Carrel': '1'}).status_code == 422
    assert display_name(config.data_dir, 'method-test') == 'Method-X'
    assert client.post(endpoint, json={**payload, 'label': ''},
                       headers={'X-Carrel': '1'}).status_code == 200
    assert display_name(config.data_dir, 'method-test') == ''


def test_mainline_name_requires_existing_branch_and_preserves_node_names(config, db):
    from carrel.graph.display import branch_names, save_display_name, display_name
    client, service = client_with_stub(config, db)
    service.explore = lambda **kwargs: {'status': 'ready', 'groups': [{'id': 'branch:abc'}]}
    save_display_name(config.data_dir, 'p', 'Method')
    endpoint, payload = '/api/graph/branch-label', {'branch_id': 'branch:abc', 'label': 'Research question'}
    assert client.post(endpoint, json=payload).status_code == 403
    assert client.post(endpoint, json=payload, headers={'X-Carrel': '1'}).status_code == 200
    assert branch_names(config.data_dir) == {'branch:abc': 'Research question'}
    assert display_name(config.data_dir, 'p') == 'Method'
    assert client.post(endpoint, json={**payload, 'branch_id': 'branch:missing'}, headers={'X-Carrel': '1'}).status_code == 404
    assert client.post(endpoint, json={**payload, 'label': 'x'*61}, headers={'X-Carrel': '1'}).status_code == 422
