"""Library navigation and citation ordering use local data, without model calls."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from carrel.relate.link import RelationCollector
from carrel.store.paths import atomic_write_json, paper_dir
from carrel.web.app import create_app


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.rows = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.rows.append((tag, dict(attrs)))


@pytest.fixture
def library(config, db):
    for pid, title, discipline, field in [
        ('old', 'Older popular paper', '计算机科学', '机器学习'),
        ('recent', 'Recent paper', '计算机科学', '自然语言处理'),
        ('zero', 'Zero cited paper', '物理学', '量子物理'),
        ('unknown', 'Unknown citations', '物理学', '量子物理'),
    ]:
        db.upsert_paper(dict(id=pid, title=title, status='indexed',
                            discipline=discipline, field=field))
    atomic_write_json(paper_dir(config.data_dir, 'old') / 'relations.json',
                      {'cited_by_count': 1200})
    atomic_write_json(paper_dir(config.data_dir, 'zero') / 'relations.json',
                      {'cited_by_count': 0})
    db.replace_relations('recent', 'cited_by', [
        dict(dst_id='citing-a', provider='openalex'),
        dict(dst_id='citing-a', provider='other'),
        dict(dst_id='citing-b', provider='openalex'),
    ])
    with db.transaction() as conn:
        for cid, name in [('set', '精读清单'), ('unassigned', '未归类')]:
            conn.execute('INSERT INTO collections(id,name,created_at) VALUES(?,?,?)',
                         (cid, name, '2026-01-01'))
        conn.executemany('INSERT INTO paper_collections(paper_id,collection_id,rank) VALUES(?,?,?)',
                         [('old', 'set', 2), ('recent', 'set', 1)])
        conn.executemany('INSERT INTO tags(paper_id,tag) VALUES(?,?)',
                         [('old', f'keyword-{i:02d}') for i in range(30)])
    with TestClient(create_app(config, db)) as client:
        yield client


def test_citations_sort_uses_total_and_distinct_fallback(library):
    papers = library.get('/api/papers?sort=citations').json()['papers']
    assert [p['id'] for p in papers[:2]] == ['old', 'recent']
    metrics = {p['id']: p for p in papers}
    assert metrics['old']['cited_by_count'] == 1200
    assert metrics['recent']['cited_by_count'] == 2
    assert metrics['zero']['citation_total_known'] is True
    assert metrics['unknown']['citation_total_known'] is False
    filtered = library.get('/api/papers', params={
        'sort': 'citations', 'collection': 'set', 'discipline': '计算机科学'}).json()['papers']
    assert [p['id'] for p in filtered] == ['old', 'recent']
    page = library.get('/?sort=citations').text
    assert page.index('Older popular paper') < page.index('Recent paper')
    assert '被引 1200' in page and '被引记录 2' in page and '引用量暂无数据' in page


def test_facets_show_only_selected_category_children(library):
    initial = Elements(library.get('/').text)
    assert not any('sub' in attrs.get('class', '').split() for _, attrs in initial.rows)
    selected = Elements(library.get('/', params={'discipline': '计算机科学'}).text)
    children = [attrs for tag, attrs in selected.rows
                if tag == 'a' and 'sub' in attrs.get('class', '').split()]
    assert {parse_qs(urlparse(a['href']).query)['field'][0] for a in children} == {
        '机器学习', '自然语言处理'}
    panels = {attrs['id']: attrs for _, attrs in selected.rows
              if attrs.get('role') == 'tabpanel'}
    assert 'hidden' not in panels['facet-category']
    assert 'hidden' in panels['facet-collection'] and 'hidden' in panels['facet-keyword']
    page = library.get('/?facet=keyword').text
    assert 'keyword-29' in page
    assert '未归类' not in library.get('/').text
    assert '精读清单' in library.get('/').text


def test_sort_form_and_category_links_preserve_search(library):
    page = library.get('/', params={'q': 'paper', 'sort': 'citations', 'status': 'indexed'}).text
    rows = Elements(page).rows
    sort = next(attrs for tag, attrs in rows if tag == 'select' and attrs.get('name') == 'sort')
    assert sort['form'] == 'library-search'
    links = [attrs for tag, attrs in rows if tag == 'a' and 'filter-link' in attrs.get('class', '')]
    assert any(attrs.get('href') == '/' for attrs in links)
    for attrs in links:
        if attrs['href'] == '/':
            continue
        query = parse_qs(urlparse(attrs['href']).query)
        assert query['q'] == ['paper'] and query['status'] == ['indexed']
        if 'discipline' in query:
            assert query['sort'] == ['citations'] and query['facet'] == ['category']


def test_collection_retains_total_citations_when_list_is_capped():
    class Metadata:
        def resolve(self, **kwargs):
            return {'id': 'https://openalex.org/W1', 'cited_by_count': 900}

        def cited_works(self, *args, **kwargs):
            return []

        def citing_works(self, *args, **kwargs):
            return []

    bundle = RelationCollector(openalex=Metadata()).collect(paper_id='old', title='Paper')
    assert bundle.to_dict()['cited_by_count'] == 900
    assert bundle.cited_by == []
