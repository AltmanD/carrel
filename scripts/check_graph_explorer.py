"""Offline graph scale probe. Creates only a fresh synthetic library.

Run with --browser for Playwright screenshots and responsive navigation checks.
Reports service timings separately from browser timings; no production URL accepted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import gzip
import json
import math
from pathlib import Path
import socket
import tempfile
import threading
import time
from unittest.mock import patch

from carrel.config import AppConfig
from carrel.graph.service import GraphService
from carrel.store.db import Database


def seed(db, count):
    for i in range(count):
        db.upsert_paper({'id': f'qa-{i:04d}', 'title': f'Method {i:04d} 中文研究', 'status': 'indexed'})
    with db.transaction() as conn:
        for group in range(8):
            conn.execute('INSERT INTO collections(id,name,sort,created_at) VALUES(?,?,?,?)',
                         (f'group-{group}', f'Reading group {group}', group, '2026-01-01'))
        conn.executemany('INSERT INTO paper_collections(paper_id,collection_id,rank) VALUES(?,?,?)',
                         [(f'qa-{i:04d}', f'group-{i % 8}', i) for i in range(count) if i % 10])
        conn.executemany('INSERT INTO tags(paper_id,tag) VALUES(?,?)',
                         [(f'qa-{i:04d}', 'even' if i % 2 == 0 else 'odd') for i in range(count)])
    db.replace_relations('qa-0000', 'cites', [{'dst_id': f'qa-{i:04d}'} for i in range(1, 31)])


def ready(service, **kwargs):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = service.explore(**kwargs)
        if result['status'] == 'error':
            raise AssertionError(result)
        if result['status'] == 'ready' and not result.get('rebuilding'):
            return result
        time.sleep(.01)
    raise AssertionError('Snapshot did not become ready in 60 seconds')


@contextmanager
def count_file_access():
    counts = {'open': 0, 'stat': 0}
    original_open, original_stat = Path.open, Path.stat
    def opened(path, *args, **kwargs):
        counts['open'] += 1
        return original_open(path, *args, **kwargs)
    def stated(path, *args, **kwargs):
        counts['stat'] += 1
        return original_stat(path, *args, **kwargs)
    with patch.object(Path, 'open', opened), patch.object(Path, 'stat', stated):
        yield counts


def benchmark(service, count, samples=20):
    started = time.perf_counter()
    overview = ready(service, group_by='collection')
    build_ms = (time.perf_counter() - started) * 1000
    assert overview['total'] == count
    assert 'graph' not in overview
    assert len(overview['groups']) <= 9
    assert all(len(g['representatives']) <= 3 for g in overview['groups'])
    raw = json.dumps(overview, ensure_ascii=False).encode()
    assert len(raw) <= 100_000
    durations = []
    with count_file_access() as reads:
        for _ in range(samples):
            started = time.perf_counter()
            result = service.explore(group_by='collection')
            durations.append((time.perf_counter() - started) * 1000)
            assert result['revision'] == overview['revision']
    assert reads == {'open': 0, 'stat': 0}, reads
    group = service.explore(mode='group')
    assert len(group['graph']['nodes']) == 24
    assert group['graph']['edges'] == []
    neighbors = service.explore(mode='neighbors', focus='qa-0000')
    assert len(neighbors['graph']['nodes']) == 6
    assert neighbors['neighbor_total'] == 30
    return {'papers': count, 'initial_projection_ms': build_ms,
            'hot_service_ms': durations, 'hot_service_p95_ms': sorted(durations)[math.ceil(.95 * samples)-1],
            'path_access_hot': reads, 'overview_bytes': len(raw),
            'overview_gzip_bytes': len(gzip.compress(raw)), 'groups': len(overview['groups']),
            'group_nodes': 24, 'neighbor_nodes': 6}


def browser_checks(config, db, output):
    import uvicorn
    from playwright.sync_api import sync_playwright
    from carrel.web.app import create_app
    app = create_app(config, db)
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
        base = f'http://127.0.0.1:{port}'
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url))
            page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
            timings = []
            for width in (1440, 1024, 768, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                for theme in ('light', 'dark'):
                    started = time.perf_counter()
                    page.goto(base + '/relations?view=graph&group_by=collection')
                    page.wait_for_selector('[data-group]')
                    timings.append((time.perf_counter() - started) * 1000)
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    assert page.locator('[data-group]').count() <= 9
                    assert page.locator('.graph-edge').count() == 0
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
                    page.screenshot(path=str(output / f'overview-{width}-{theme}.png'), full_page=True)
            assert not any('elk' in url.lower() for url in requests), requests
            page.locator('[data-group]').first.focus()
            page.keyboard.press('Enter')
            page.wait_for_function('window.CarrelGraph?.last?.nodes.size > 0')
            assert page.evaluate('window.CarrelGraph.last.nodes.size') <= 24
            assert page.locator('.graph-edge').count() == 0
            page.reload()
            page.wait_for_function('window.CarrelGraph?.last?.nodes.size > 0')
            assert 'mode=group' in page.url
            assert not errors, errors
            browser.close()
            return {'overview_navigation_ms': timings, 'page_errors': errors}
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        app.state.graphs.close()
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('probe-out/graph-explorer-qa'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'scope': 'Synthetic indexed papers; service timings exclude HTTP. No cold-process p95 claim.', 'runs': []}
    for count in (500, 1000):
        with tempfile.TemporaryDirectory(prefix='graph-qa-', dir=args.output) as directory:
            config = AppConfig(data_dir=Path(directory))
            db = Database(config.data_dir / 'app.db')
            service = GraphService(db, config.data_dir)
            try:
                seed(db, count)
                run = benchmark(service, count)
                if args.browser:
                    output = args.output / str(count)
                    output.mkdir(exist_ok=True)
                    run['browser'] = browser_checks(config, db, output)
                report['runs'].append(run)
            finally:
                service.close()
                db.close()
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
