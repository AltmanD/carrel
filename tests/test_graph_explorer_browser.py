"""Direction navigation and fuzzy location against an isolated synthetic library."""
import socket
import threading
import time

import pytest

from carrel.graph.build import _paper_node
from carrel.graph.model import Edge, Graph
from carrel.web.app import create_app


@pytest.mark.browser
def test_graph_visible_navigation_location_and_responsive_layout(config, db, monkeypatch, tmp_path):
    import uvicorn
    sync_playwright = pytest.importorskip('playwright.sync_api').sync_playwright
    for i in range(40):
        db.upsert_paper({'id': f'p{i:02}', 'title': f'Signal Method {i:02}', 'status': 'indexed',
                         'discipline': 'Artificial Intelligence' if i < 30 else 'Robotics',
                         'field': 'Agents' if i < 20 else 'Reasoning' if i < 30 else 'Control'})
    with db.transaction() as conn:
        for cid in ('reading', 'other'):
            conn.execute('INSERT INTO collections(id,name,sort,created_at) VALUES(?,?,0,?)', (cid, cid, 'now'))
        conn.executemany('INSERT INTO paper_collections(paper_id,collection_id) VALUES(?,?)',
                         [(f'p{i:02}', 'reading' if i < 30 else 'other') for i in range(40)])
    app = create_app(config, db)
    db.replace_relations('p06', 'cites', [{'dst_id':f'p{i:02}'} for i in (7,8,9,10,11,12,13,35,36,37,38,39)])
    db.replace_relations('p08', 'cites', [{'dst_id': 'p06'}])
    service = app.state.graphs
    nodes = [_paper_node(db.get_paper(f'p{i:02}'), '') for i in range(30)]
    tree = Graph(scope='motivation_tree', nodes=nodes,
                 edges=[Edge(nodes[i].id, nodes[i + 1].id, 'motivation', basis='semantic') for i in range(19)] +
                       [Edge(nodes[20].id, nodes[21].id, 'motivation', basis='semantic')])
    monkeypatch.setattr(service, 'motivation_tree', lambda: tree)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=[f'--explicitly-allowed-ports={port}'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors, requests = [], []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('request', lambda r: requests.append(r.url))
            base = f'http://127.0.0.1:{port}'
            page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())

            def settled():
                page.wait_for_function("document.querySelector('[data-graph-explorer]').getAttribute('aria-busy') === 'false' && document.querySelector('.explorer-state').dataset.state === 'ready'")

            page.goto(base + '/relations')
            settled()
            assert 'mode=group' in page.url
            assert page.locator('.graph-node').count() == 24
            page.screenshot(path=str(tmp_path / 'relations-desktop.png'), full_page=True)
            assert page.locator('[data-graph-canvas]').bounding_box()['y'] < 330
            assert page.locator('[name=q], [data-cards], [data-facets]').count() == 0
            assert not any('elk.bundled' in url for url in requests)
            original = page.evaluate("[...CarrelGraph.last.nodes.keys()]")
            geometry = page.evaluate("[...CarrelGraph.last.nodes.values()].map(n=>[n.id,n.x,n.y])")
            viewport = page.evaluate('({...CarrelGraph.last.transform})')
            page.locator('[data-node-id="paper:p06"]').click()
            page.wait_for_function("document.querySelector('[data-graph-explorer]').dataset.citationState === 'ready'")
            assert page.locator('[data-detail]').is_visible()
            assert page.evaluate('CarrelGraph.last.nodes.size') == len(original)+5
            assert page.evaluate("CarrelGraph.last.edges.filter(e=>e.role==='cites').length") == 13
            assert page.evaluate('(before)=>before.every(([id,x,y])=>CarrelGraph.last.nodes.get(id).x===x&&CarrelGraph.last.nodes.get(id).y===y)', geometry)
            assert page.evaluate('({...CarrelGraph.last.transform})') == viewport
            assert page.locator('.graph-node.citation-context').count() > 0
            # Dragging the background pans, preserving detail and citations.
            canvas_box = page.locator('[data-graph-canvas]').bounding_box()
            page.mouse.move(canvas_box['x'] + 600, canvas_box['y'] + 10)
            page.mouse.down()
            page.mouse.move(canvas_box['x'] + 620, canvas_box['y'] + 25, steps=4)
            page.mouse.up()
            assert page.locator('[data-detail]').is_visible()
            assert page.evaluate('CarrelGraph.last.citationOverlay !== null')
            page.locator('[data-detail-close]').click()
            assert page.locator('[data-detail]').is_hidden()
            assert page.evaluate("CarrelGraph.last.edges.some(e=>e.role==='cites')")
            page.locator('[data-node-id="paper:p07"]').click()
            page.wait_for_function("document.querySelector('[data-graph-explorer]').dataset.citationState === 'ready' && CarrelGraph.last.selected === 'paper:p07'")
            assert page.evaluate('CarrelGraph.last.nodes.size') == 24
            assert page.evaluate('CarrelGraph.last.edges.length') == 1
            page.locator('[data-detail-close]').click()
            page.locator('[data-graph-canvas]').click(position={'x':10,'y':10})
            page.wait_for_function("CarrelGraph.last.nodes.size === 24 && !CarrelGraph.last.selected")
            assert not page.evaluate("CarrelGraph.last.edges.some(e=>e.role==='cites')")
            assert page.locator('[data-detail]').is_hidden()
            # A background tap also cancels an in-flight expansion.
            def delayed(route):
                time.sleep(.1)
                route.continue_()
            page.route('**/api/graph/citations?*', delayed)
            page.locator('[data-node-id="paper:p06"]').click()
            page.locator('[data-graph-canvas]').click(position={'x':10,'y':10})
            page.wait_for_timeout(200)
            assert page.evaluate('CarrelGraph.last.nodes.size') == 24
            assert page.locator('[data-detail]').is_hidden()
            page.unroute('**/api/graph/citations?*', delayed)
            page.screenshot(path=str(tmp_path / 'relations-desktop.png'), full_page=True)

            page.locator('[data-overview]').click()
            settled()
            assert page.locator('.graph-node.aggregate').count() == 2
            assert page.locator('.graph-edge').count() == 0
            page.locator('[data-graph-canvas]').focus()
            page.keyboard.press('ArrowRight')
            assert 'mode=overview' in page.url
            page.keyboard.press('Enter')
            settled()
            assert 'mode=group' in page.url
            paper = page.evaluate("[...CarrelGraph.last.nodes.values()][0].meta.paper_id")
            query = 'Signal Method ' + paper[1:]
            count = page.evaluate('CarrelGraph.last.nodes.size')
            page.locator('[data-locate-input]').fill(query)
            page.wait_for_selector('[data-locate-hit]')
            page.locator('[data-locate-hit]').first.click()
            assert page.evaluate('CarrelGraph.last.nodes.size') == count
            assert 'mode=group' in page.url
            assert page.evaluate('CarrelGraph.last.selected')

            page.locator('[data-locate-input]').fill('Signal Method 39')
            page.wait_for_selector('[data-locate-hit]')
            page.locator('[data-locate-hit]').first.click()
            settled()
            assert 'mode=neighbors' in page.url and 'focus=p39' in page.url
            assert page.locator('[data-back]').is_visible()
            page.locator('[data-back]').click()
            settled()
            assert 'mode=group' in page.url

            page.locator('[data-view=tree]').click()
            settled()
            assert 'mode=branch' in page.url
            assert page.evaluate('CarrelGraph.last.nodes.size') == 12
            assert page.locator('.graph-node').count() > 0
            assert page.locator('[data-processing-message]').is_visible()
            assert page.locator('[data-processing-message]').get_attribute('open') is None
            colors = page.locator('.explorer-view-switch button').evaluate_all('(items)=>items.map(b=>getComputedStyle(b).backgroundColor)')
            assert colors[0] != colors[1]
            page.screenshot(path=str(tmp_path / 'lineage-desktop.png'), full_page=True)
            page.locator('[data-overview]').click()
            settled()
            assert page.locator('.graph-node.aggregate').count() == 2
            page.locator('[data-direction]').first.click()
            settled()
            assert page.locator('.graph-node.aggregate').count() == 2
            page.locator('.graph-node.aggregate').first.click()
            settled()
            assert page.evaluate('CarrelGraph.last.edges.length') == 11
            assert page.locator('.graph-edge[marker-end]').count() > 0
            page.locator('.graph-node').first.click()
            assert page.locator('[data-detail]').is_visible()
            page.locator('[data-detail-close]').click()
            assert page.evaluate('CarrelGraph.last.edges.length') == 11
            page.locator('.graph-node').first.click()
            page.locator('[data-graph-canvas]').click(position={'x':10,'y':10})
            assert page.locator('[data-detail]').is_hidden()
            assert not page.evaluate('CarrelGraph.last.selected')
            assert page.evaluate('CarrelGraph.last.edges.length') == 11
            page.set_viewport_size({'width': 390, 'height': 844})
            page.wait_for_function("Math.abs(CarrelGraph.last.canvas.viewBox.baseVal.width - CarrelGraph.last.canvas.clientWidth) < 2")
            assert page.evaluate('CarrelGraph.last.transform.k') >= .85

            for theme in ('light', 'dark'):
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.goto(base + '/relations?view=tree&mode=overview')
                settled()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
                assert page.locator('[data-graph-canvas]').bounding_box()['y'] < 440
                page.screenshot(path=str(tmp_path / f'relations-mobile-{theme}.png'), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        service.close()
