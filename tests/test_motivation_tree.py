"""Offline lineage validation, incremental construction, jobs, and web projection."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from carrel.graph.service import GraphService
from carrel.model import ModelError, ModelReply
from carrel.motivation import tree
from carrel.motivation.extract import ensure_motivation
from carrel.store.paths import atomic_write_json, paper_dir, read_json
from carrel.web.app import _JobRunner, create_app
from tests.conftest import FakeBackend
from tests.test_motivation import seed_motivation_source


def edge(a, b, confidence=.9, cited=False, **extra):
    return {"from": a, "to": b, "relation": "extends", "confidence": confidence,
            "cited": cited, "evidence": "后续工作将人工课程扩展为自动课程", **extra}


class TreeBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.fail_tree = False

    def structured(self, **kwargs):
        if kwargs.get("task") != "motivation_tree":
            return super().structured(**kwargs)
        if self.fail_tree:
            raise ModelError("simulated tree outage")
        request = json.loads(kwargs["user"])
        self.requests.append(request)
        papers = request["candidates"] + request["new_papers"]
        new = {p["id"] for p in request["new_papers"]}
        edges = [edge(a["id"], b["id"], cited=[b["id"], a["id"]] in request["cites_pairs"])
                 for a, b in zip(papers, papers[1:]) if a["id"] in new or b["id"] in new]
        data = {"edges": edges}
        return ModelReply(text=json.dumps(data), data=data, tokens_in=200, tokens_out=60,
                          cost_usd=.001, task="motivation_tree")


def seed_library(config, db, count=3):
    papers = [seed_motivation_source(config, db, f"arxiv:24{i % 12 + 1:02d}.{i + 1:05d}")[0]
              for i in range(count)]
    for paper in papers:
        ensure_motivation(config, db, FakeBackend(), paper)
    return papers


def test_month_from_new_and_legacy_arxiv():
    assert tree.month_of({"id": "arxiv:2611.01234"}) == "2026-11"
    assert tree.month_of({"id": "arxiv:hep-th/9901001"}) == "1999-01"
    assert tree.month_of({"id": "arxiv:2413.00001"}) == ""
    assert tree.month_of({"id": "pdf-123", "year": 2024}) == ""


def test_validation_drops_bad_endpoints_and_duplicates_but_keeps_missing_dates():
    papers = {pid: {"id": pid, "year": None} for pid in ["a", "b", "c"]}
    raw = [edge("a", "a"), edge("a", "outside"), edge("a", "b", .8),
           edge("a", "b", .95), edge("b", "c", confidence=float("nan")),
           edge("b", "c", evidence=""), {"from": [], "to": "a"}]
    edges, notes = tree.validate_edges(raw, papers, set())
    assert len(edges) == 1 and edges[0]["confidence"] == .95
    assert any("月份未知" in note for note in notes)
    assert any("自环" in note for note in notes) and any("重复" in note for note in notes)


def test_suspicious_month_direction_is_a_note_not_a_rejection():
    a, b = "arxiv:2412.00001", "arxiv:2401.00002"
    edges, notes = tree.validate_edges([edge(a, b)], {pid: {"id": pid} for pid in [a, b]}, set())
    assert len(edges) == 1 and any("顺序相反" in n for n in notes)


def test_cycles_drop_lowest_confidence_and_citations_require_both_checks():
    papers = {pid: {"id": pid} for pid in ["a", "b", "c", "d"]}
    edges, notes = tree.validate_edges([edge("a", "b", .95, True), edge("b", "c", .9),
              edge("c", "a", .8), edge("a", "d", .9, True)], papers, {("b", "a"), ("c", "b")})
    by_pair = {(e["from"], e["to"]): e for e in edges}
    assert ("c", "a") not in by_pair
    assert by_pair["a", "b"]["basis"] == "citation"
    assert by_pair["b", "c"]["basis"] == "semantic"  # model did not claim cited
    assert by_pair["a", "d"]["basis"] == "semantic"
    assert any("循环" in n for n in notes) and any("没有对应方向" in n for n in notes)


def test_citation_union_and_pdf_identifier_gap(config, db):
    papers = seed_library(config, db, 4)
    a, b, c, d = [p["id"] for p in papers]
    seed_motivation_source(config, db, "pdf-abc")
    db.replace_relations(b, "cites", [{"dst_id": a}])
    db.replace_relations(c, "cites", [{"dst_id": "openalex:W1", "dst_arxiv": b[6:] + "v2"}])
    db.replace_relations(c, "cited_by", [{"dst_id": d, "provider": "openalex"}])
    db.replace_relations("pdf-abc", "cites", [{"dst_id": "openalex:unknown", "dst_title": papers[0]["title"]}])
    assert tree.citation_pairs(db) == {(b, a), (c, b), (d, c)}


def test_rebuild_reads_ready_papers_then_incrementally_adds_changed_papers(config, db):
    papers = seed_library(config, db)
    backend = TreeBackend()
    result = tree.rebuild_tree(config, db, backend)
    assert result["stats"]["papers_with_motivation"] == 3
    assert len(backend.requests) == 1 and len(result["edges"]) == 2
    assert len([c for c in backend.calls if c["task"] == "motivation"]) == 0
    assert tree.extend_tree(config, db, backend) == result
    assert len(backend.requests) == 1
    new, _ = seed_motivation_source(config, db, "arxiv:2501.00001")
    ensure_motivation(config, db, FakeBackend(), new)
    db.replace_relations(new["id"], "cites", [{"dst_id": papers[0]["id"]}])
    result = tree.extend_tree(config, db, backend, [new["id"]])
    assert len(backend.requests) == 2
    assert [p["id"] for p in backend.requests[-1]["new_papers"]] == [new["id"]]
    assert result["stats"]["papers_with_motivation"] == 4
    base = paper_dir(config.data_dir, papers[0]["id"])
    summary = read_json(base / "summary.json")
    summary["one_line"] += " revised"
    atomic_write_json(base / "summary.json", summary)
    ensure_motivation(config, db, FakeBackend(), papers[0])
    tree.extend_tree(config, db, backend)
    assert [p["id"] for p in backend.requests[-1]["new_papers"]] == [papers[0]["id"]]


def test_rebuild_is_batched_and_bounded_at_fifty_papers(config, db):
    seed_library(config, db, 51)
    backend = TreeBackend()
    result = tree.rebuild_tree(config, db, backend)
    assert result["stats"]["papers_with_motivation"] == 51
    assert len(backend.requests) == 6
    assert all(len(r["new_papers"]) <= 10 and len(r["candidates"]) <= 20 for r in backend.requests)
    assert max(len(json.dumps(r, ensure_ascii=False)) for r in backend.requests) < 18000


def test_failure_preserves_old_tree_and_cost_has_no_phantom_paper(config, db):
    seed_library(config, db)
    backend = TreeBackend()
    tree.rebuild_tree(config, db, backend)
    before = (config.data_dir / "motivation_tree.json").read_bytes()
    backend.fail_tree = True
    with pytest.raises(ModelError):
        tree.rebuild_tree(config, db, backend)
    assert (config.data_dir / "motivation_tree.json").read_bytes() == before
    runs = db.runs_for("@library")
    assert any(r["status"] == "failed" for r in runs)
    assert all("_library" in r["trace_dir"] for r in runs)
    assert db.usage_summary()["total"]["papers"] == 3
    assert not (config.data_dir / "papers" / "@library").exists()


def test_projection_focus_isolated_deleted_nodes_and_cache(config, db):
    papers = seed_library(config, db)
    tree.rebuild_tree(config, db, TreeBackend())
    service = GraphService(db, config.data_dir)
    graph = service.motivation_tree(papers[1]["id"])
    assert graph.scope == "motivation_tree" and graph.focus
    assert all(e.role == "motivation" and e.basis == "semantic" for e in graph.edges)
    assert graph.stats["isolated"] == 0
    neighbours = service.motivation_neighbors(papers[1]["id"])
    assert len(neighbours["upstream"]) == len(neighbours["downstream"]) == 1
    db.execute("DELETE FROM papers WHERE id=?", (papers[1]["id"] ,))
    graph = service.motivation_tree()
    assert not graph.nodes and not graph.edges and graph.stats["isolated"] == 2
    assert any("移出" in note for note in graph.notes)


def test_library_queue_dedup_and_imports_have_separate_workers(config, db, monkeypatch):
    import threading
    runner = _JobRunner(config, db)
    started, release = threading.Event(), threading.Event()
    def build(*args):
        started.set()
        assert release.wait(5)
    monkeypatch.setattr(tree, 'extend_tree', build)
    runner.scheduler.finalizer = lambda batch: None
    runner.scheduler.pipeline.process = lambda *a, **kw: 'arxiv:2401.00001'
    first = runner.submit('', kind='motivation_tree')
    try:
        assert started.wait(2)
        assert runner.submit('', kind='motivation_tree')['job_id'] == first['job_id']
        job = runner.submit('arxiv:2401.00001')
        import time
        deadline = time.monotonic() + 3
        while runner.scheduler.status(job['batch_id'])['items'][0]['status'] != 'succeeded' and time.monotonic() < deadline:
            time.sleep(.01)
        assert runner.scheduler.status(job['batch_id'])['items'][0]['status'] == 'succeeded'
    finally:
        release.set()
        runner.scheduler.wait(job['batch_id'], timeout=5)
        runner.scheduler.shutdown()


def test_web_empty_rebuild_conflict_and_sentinel_links(config, db, monkeypatch):
    app = create_app(config, db)
    monkeypatch.setattr(app.state.jobs.scheduler, "_start", lambda: None)
    client = TestClient(app)
    empty = client.get("/api/motivation-tree").json()
    assert empty["scope"] == "motivation_tree" and not empty["stats"]["rebuilding"]
    assert "重新梳理" in empty["notes"][0]
    assert client.post("/api/motivation-tree/rebuild").status_code == 200
    assert client.post("/api/motivation-tree/rebuild").status_code == 409
    assert client.get("/api/motivation-tree").json()["stats"]["rebuilding"]
    page = client.get("/relations?view=tree").text
    assert 'data-graph-explorer' in page and 'data-graph-payload' not in page
    for url in ("/jobs", "/import"):
        html = client.get(url).text
        assert "全库 · 动机脉络" in html and "/papers/@library" not in html


def test_paper_motivation_card_position_and_missing_state(config, db):
    papers = seed_library(config, db)
    tree.rebuild_tree(config, db, TreeBackend())
    pid = papers[1]["id"]
    base = paper_dir(config.data_dir, pid)
    summary = read_json(base / "summary.json")
    summary["contributions"] = ["Contribution"]
    atomic_write_json(base / "summary.json", summary)
    ensure_motivation(config, db, FakeBackend(), papers[1])
    client = TestClient(create_app(config, db))
    html = client.get(f"/papers/{pid}").text
    assert html.index("论文速览") < html.index("核心贡献") < html.index("motivation-card")
    assert 'class="assessment motivation-card" open' in html
    assert "查看研究脉络" in html and "view=tree&focus=" in html
    (base / "motivation.json").unlink()
    assert "motivation-card" not in client.get(f"/papers/{pid}").text
