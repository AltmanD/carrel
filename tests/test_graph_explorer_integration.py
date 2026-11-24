"""Independent scale and HTTP integration checks on synthetic libraries."""
import pytest
from fastapi.testclient import TestClient

from carrel.graph.service import GraphService
from carrel.web.app import create_app
from scripts.check_graph_explorer import benchmark, count_file_access, ready, seed


@pytest.mark.parametrize('count', [500, 1000])
def test_scale_bounded_overview_and_zero_hot_file_access(config, db, count):
    seed(db, count)
    service = GraphService(db, config.data_dir)
    try:
        result = benchmark(service, count)
        assert len(result['hot_service_ms']) >= 20
    finally:
        service.close()


def test_real_api_etag_filters_and_equal_count_relation_edit(config, db):
    seed(db, 64)
    app = create_app(config, db)
    service = app.state.graphs
    try:
        ready(service)
        client = TestClient(app)
        url = '/api/graph/explore?group_by=collection'
        first = client.get(url)
        assert first.status_code == 200
        assert first.json()['total'] == 64
        with count_file_access() as reads:
            cached = client.get(url, headers={'If-None-Match': first.headers['etag']})
        assert cached.status_code == 304
        assert reads['open'] == 0
        filtered = client.get(url + '&collection=group-1&keyword=even')
        assert filtered.json()['total'] == 0
        before = service.node_detail('qa-0000')['counts']['cites']
        db.replace_relations('qa-0000', 'cites', [{'dst_id': f'qa-{i:04d}'} for i in range(2, 32)])
        ready(service)
        after = client.get(url, headers={'If-None-Match': first.headers['etag']})
        assert after.status_code == 200
        assert after.headers['etag'] != first.headers['etag']
        assert service.node_detail('qa-0000')['counts']['cites'] == before
        assert service.node_detail('qa-0001')['counts']['cited_by'] == 0
        assert service.node_detail('qa-0031')['counts']['cited_by'] == 1
    finally:
        service.close()
