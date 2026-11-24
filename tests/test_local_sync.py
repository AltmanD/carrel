"""Offline sync preserves identities, curated data and transaction boundaries."""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carrel.ids import sha256_file
from carrel.store.db import Database
from carrel.store.paths import paper_dir
from carrel.sync.apply import apply_scan
from carrel.sync.collections import collection_for, rules_text
from carrel.sync.scanner import scan
from carrel.web.app import create_app


def pdf(root, relative, content=b"%PDF-1.4 local fixture"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "reading"
    a = pdf(root, "PRM-reading/03_Lets_Verify_2305.20050.pdf")
    pdf(root, "能力域论文库_20261022/01_数学/2305.20050.pdf")
    pdf(root, "Loose_Paper.PDF", b"%PDF-1.4 another")
    pdf(root, "code/ignore.pdf")
    pdf(root, "repos/ignore.pdf")
    pdf(root, "framework_source_snapshots_20261016/ignore.pdf")
    (root / "empty").mkdir()
    write_json(a.parent / "papers.json", {"papers": [{"arxiv_id": "2305.20050", "title": "Let's Verify Step by Step",
        "authors": ["A", "B"], "published": "2023-05-31", "summary": "Process supervision", "pdf": a.name}]})
    (a.parent / "README.md").write_text(
        "| 编号 | 论文（PDF） | 首次提交年份 | 类型 | 为什么读 |\n|---|---|---|---|---|\n"
        f"| 03 | [Let's Verify]({a.name}) | 2023 | 必读 | 理解逐步验证。 |\n", encoding="utf-8")
    (root / "能力域论文库_20261022/01_数学/README.md").write_text(
        "| 论文 | arXiv | 定位 |\n|---|---|---|\n| Let's Verify | 2305.20050 | 过程监督 |\n", encoding="utf-8")
    return root


def test_schema_upgrade_backup_preserves_existing_rows(tmp_path):
    path = tmp_path / "app.db"
    db = Database(path)
    db.upsert_paper({"id": "old", "title": "Retain me", "status": "ready"})
    db.execute("DROP TABLE notes")
    db.execute("DROP TABLE paper_collections")
    db.execute("DROP TABLE collections")
    db.execute("UPDATE schema_meta SET value='4' WHERE key='version'")
    db.close()
    db = Database(path)
    try:
        from carrel.store.db import SCHEMA_VERSION
        assert db.version == SCHEMA_VERSION
        assert db.get_paper("old")["title"] == "Retain me"
        db.init_schema()
        assert db.collections() == []
        assert {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")} >= {"idx_notes_paper", "idx_pc_collection"}
        backup = next((tmp_path / "_backup").glob("schema-v4-*.db"))
        with sqlite3.connect(backup) as connection:
            assert connection.execute("SELECT title FROM papers").fetchone()[0] == "Retain me"
    finally:
        db.close()


def test_scanner_deduplicates_without_losing_memberships(library):
    before = {str(p) for p in library.rglob("*")}
    result = scan(library)
    assert result.pdf_count == 3
    assert len(result.papers) == 2
    paper = next(p for p in result.papers if p["arxiv_id"])
    assert paper["title"] == "Let's Verify Step by Step"
    assert paper["year"] == 2023
    assert paper["abstract"] == "Process supervision"
    assert len(paper["sources"]) == 2
    assert len({s["collection_id"] for s in paper["sources"]}) == 2
    assert {s["rank"] for s in paper["sources"]} == {3, None}
    assert len(result.notes) == 2
    assert len(result.skipped) == 4
    assert before == {str(p) for p in library.rglob("*")}


@pytest.mark.parametrize("shape", ["paper_index", "batch", "single", "nested"])
def test_metadata_schemas(tmp_path, shape):
    path = pdf(tmp_path, "topic/papers/2401.12345.pdf")
    if shape == "paper_index":
        write_json(path.parent.parent / "paper_index.json", [{"file": "papers\\2401.12345.pdf", "metadata": {
            "citation_title": ["Real Title"], "citation_author": ["One"], "citation_date": ["2024/01/12"],
            "citation_arxiv_id": ["2401.12345"], "citation_abstract": ["Real Abstract"]}}])
    else:
        row = {"arxiv_id": "2401.12345v2", "title": "Real Title", "abstract": "Real Abstract", "authors": ["One"], "date": "2024-01-12"}
        if shape == "batch":
            row["pdf"] = "papers/2401.12345.pdf"
            write_json(path.parent.parent / "sources.json", {"papers": [row]})
        elif shape == "single":
            write_json(path.parent / "sources.json", row)
        else:
            (path.parent / "sources").mkdir()
            write_json(path.parent / "sources/metadata.json", row)
    paper = scan(tmp_path).papers[0]
    assert (paper["title"], paper["year"], paper["arxiv_id"], paper["abstract"]) == ("Real Title", 2024, "2401.12345", "Real Abstract")


def test_mapping_overrides_and_new_folders(library, tmp_path):
    initial = scan(library)
    rules = tmp_path / "collections.toml"
    rules.write_text(rules_text(initial.collections).replace('name = "PRM"', 'name = "Process Rewards"'), encoding="utf-8")
    pdf(library, "New-reading/new.pdf", b"%PDF new")
    updated = scan(library, collections_path=rules)
    assert {c["name"] for c in updated.collections} >= {"Process Rewards", "New"}
    root_collection, rank = collection_for(library, library / "01_root.pdf", [])
    assert root_collection["name"] == "未归类" and rank == 1


def test_reading_tables_sections_and_unmatched(tmp_path):
    pdf(tmp_path, "topic/2401.12345.pdf")
    pdf(tmp_path, "topic/2402.12345.pdf", b"%PDF other")
    (tmp_path / "topic/README.md").write_text(
        "| 顺序 | 材料与本地位置 | 为什么现在读 | 读完必须写出的决定 |\n|---|---|---|---|\n"
        "| 1 / P0 | [Paper](2401.12345.pdf) | 保留原文措辞 | 输出决定 |\n"
        "| P1 | [Web](articles/a.html) | 读网页 | A |\n\n"
        "## 阶段一：基础\n### `2402.12345` Paper Two\n- **为什么读**：理解机制\n- **重点**：保留公式\n\n"
        "### 第 2 组：系统\n1. `2401.12345.pdf`\n   - 重点看性能。\n", encoding="utf-8")
    result = scan(tmp_path)
    assert len(result.notes) == 2
    first = next(n for n in result.notes if n["paper_id"] == "arxiv:2401.12345")
    assert first["priority"] == "P0"
    assert "输出决定" in first["body"] and "重点看性能" in first["body"]
    assert "#L3" in first["source_ref"]
    assert len(result.unmatched_notes) == 1
    assert "Web" in result.unmatched_notes[0]["text"]


def test_note_via_reading_guide_requires_unique_pdf(tmp_path):
    pdf(tmp_path, "reading/unit/a.pdf")
    (tmp_path / "reading/README.md").write_text(
        "| 顺序 | 中文阅读说明 | 阅读目的 |\n|---|---|---|\n| 1 | [Guide](unit/阅读说明.md) | 理解方法 |\n", encoding="utf-8")
    assert len(scan(tmp_path).notes) == 1
    pdf(tmp_path, "reading/unit/b.pdf", b"%PDF another")
    result = scan(tmp_path)
    assert not result.notes and len(result.unmatched_notes) == 1


def test_apply_idempotent_keeps_curated_fields(library, db, data_dir):
    result = scan(library)
    first = apply_scan(result, db, data_dir)
    assert first["added"] == 2 and first["notes_added"] == 2
    assert (Path(first["backup"]) / "app.db").is_file()
    assert all(p["pdf_sha256"] and p["status"] == "indexed" for p in db.list_papers())
    assert not (data_dir / "papers").exists()
    pid = "arxiv:2305.20050"
    note = db.notes_for(pid)[0]
    db.execute("UPDATE notes SET body='My change',source='human' WHERE paper_id=? AND collection_id=?", (pid, note["collection_id"]))
    db.execute("UPDATE papers SET title='Curated',status='ready',discipline='Custom' WHERE id=?", (pid,))
    db.execute("UPDATE collections SET name='Renamed' WHERE id=?", (note["collection_id"],))
    second = apply_scan(result, db, data_dir)
    assert second["added"] == 0 and second["existing"] == 2
    assert second["human_notes_preserved"] == 1
    assert db.get_paper(pid)["title"] == "Curated"
    assert db.get_paper(pid)["status"] == "ready"
    assert db.get_paper(pid)["discipline"] == "Custom"
    assert any(n["body"] == "My change" for n in db.notes_for(pid))
    assert any(c["name"] == "Renamed" for c in db.collections())


def test_apply_transaction_rolls_back(library, db, data_dir):
    db.execute("CREATE TRIGGER fail_note BEFORE INSERT ON notes BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
    with pytest.raises(sqlite3.IntegrityError):
        apply_scan(scan(library), db, data_dir)
    assert db.list_papers() == [] and db.collections() == []
    assert not (data_dir / "sync_report.json").exists()
    assert list((data_dir / "_backup").glob("sync-*/app.db"))


def test_apply_selection_and_stale_bytes(library, db, data_dir):
    result = scan(library)
    paper = result.papers[0]
    report = apply_scan(result, db, data_dir, selected=[paper["pdf_sha256"]])
    assert report["added"] == 1 and len(db.list_papers()) == 1
    Path(paper["source_path"]).write_bytes(b"%PDF changed")
    with pytest.raises(ValueError, match="PDF"):
        apply_scan(result, db, data_dir)
    assert len(db.list_papers()) == 1
    with pytest.raises(ValueError):
        apply_scan(result, db, data_dir, selected=["invented"])


def test_same_arxiv_different_bytes_do_not_merge(tmp_path, db, data_dir):
    pdf(tmp_path / "source", "a/2401.12345.pdf")
    pdf(tmp_path / "source", "b/2401.12345.pdf", b"%PDF different version")
    result = scan(tmp_path / "source")
    assert len({p["id"] for p in result.papers}) == 2 and result.warnings
    apply_scan(result, db, data_dir)
    assert len(db.list_papers()) == 2
    assert apply_scan(result, db, data_dir)["added"] == 0


@pytest.mark.parametrize("name", ["2401.12345.pdf", "local.pdf", "title_with_spaces.pdf"])
def test_processing_synced_paper_reuses_identity(tmp_path, db, data_dir, pipeline_factory, name):
    source = pdf(tmp_path / "source", name)
    result = scan(source.parent)
    apply_scan(result, db, data_dir)
    expected = db.list_papers()[0]["id"]
    pipeline = pipeline_factory()
    assert pipeline.process(str(source)) == expected
    assert len(db.list_papers()) == 1
    assert db.get_paper(expected)["pdf_sha256"] == sha256_file(source)
    assert (paper_dir(data_dir, expected) / "source.pdf").is_file()
    assert source.is_file()


@pytest.fixture
def web(config, db):
    app = create_app(config, db)
    submissions = []
    def submit(raw, **kwargs):
        submissions.append((raw, kwargs))
        return {"paper_id": kwargs.get("paper_id"), "job_id": "fake-job"}
    app.state.jobs.submit = submit
    with TestClient(app, headers={"X-Carrel": "1"}) as client:
        yield client, submissions


def test_web_preview_apply_notes_filters_and_processing(web, library, db, data_dir):
    client, submissions = web
    response = client.post("/api/sync/scan", json={"root": str(library)})
    assert response.status_code == 200, response.text
    result = response.json()
    assert db.list_papers() == [] and not (data_dir / "collections.toml").exists()
    apply = client.post("/api/sync/apply", json={"scan_id": result["scan_id"], "selected": [p["pdf_sha256"] for p in result["papers"]]})
    assert apply.status_code == 200, apply.text
    assert not submissions
    pid = "arxiv:2305.20050"
    page = client.get(f"/papers/{pid}")
    assert page.status_code == 200 and 'data-process-indexed=' in page.text
    assert 'id="pipe-progress"' not in page.text
    note = client.post(f"/api/papers/{pid}/notes", json={"body": "Human note", "priority": "P0"})
    assert note.status_code == 200
    assert any(n["source"] == "human" and n["body"] == "Human note" for n in note.json()["notes"])
    collection = db.collections(pid)[0]
    filtered = client.get("/api/papers", params={"collection": collection["id"]}).json()["papers"]
    assert [p["id"] for p in filtered] == [pid]
    assert client.get("/", params={"collection": collection["id"]}).status_code == 200
    for url in ("/sync", "/collections", "/import"):
        assert client.get(url).status_code == 200
    assert client.post(f"/api/papers/{pid}/retry").status_code == 200
    assert submissions[0][1]["paper_id"] == pid
    assert db.get_paper(pid)["status"] == "queued"
    assert client.delete(f"/api/papers/{pid}/notes").status_code == 200
    assert not any(n["collection_id"] == "" for n in db.notes_for(pid))


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_sync_rejects_cross_site(web, library, site):
    client, _ = web
    assert client.post("/api/sync/scan", json={"root": str(library)}, headers={"Sec-Fetch-Site": site}).status_code == 403
    assert client.post("/api/collections", json={"name": "X"}, headers={"Sec-Fetch-Site": site}).status_code == 403


def test_invalid_preview_and_sources(web, library, db, data_dir):
    client, submissions = web
    assert client.post("/api/sync/scan", json={"root": 12}).status_code == 422
    assert client.post("/api/sync/scan", json={"root": "   "}).status_code == 400
    assert client.post("/api/sync/apply", json={"scan_id": "unknown", "selected": ["x"]}).status_code == 409
    apply_scan(scan(library), db, data_dir)
    pid = "arxiv:2305.20050"
    source = Path(db.get_paper(pid)["source_path"])
    source.write_bytes(b"%PDF changed")
    assert client.post(f"/api/papers/{pid}/retry").status_code == 409
    source.unlink()
    assert client.post(f"/api/papers/{pid}/retry").status_code == 409
    assert not submissions


def test_collection_delete_preserves_papers_and_notes(web, library, db, data_dir):
    client, _ = web
    apply_scan(scan(library), db, data_dir)
    pid = "arxiv:2305.20050"
    c = db.collections(pid)[0]
    old_note = next(n for n in db.notes_for(pid) if n["collection_id"] == c["id"])
    assert client.post("/api/collections", json={"id": c["id"], "name": "New name"}).status_code == 200
    assert client.delete(f"/api/collections/{c['id']}").status_code == 200
    assert len(db.list_papers()) == 2
    general = next(n for n in db.notes_for(pid) if n["collection_id"] == "")
    assert old_note["body"] in general["body"] and "New name" in general["body"]


def test_cli_dry_run_does_not_create_library(library, tmp_path, capsys):
    from carrel.cli import main
    target = tmp_path / "not-created"
    assert main(["--data-dir", str(target), "sync", str(library), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["stats"]["papers"] == 2
    assert not target.exists()


def test_collection_reading_order(tmp_path, db, data_dir):
    root = tmp_path / "ordered"
    pdf(root, "Topic-reading/03_FirstAlphabetically.pdf", b"%PDF 3")
    pdf(root, "Topic-reading/01_ZLastAlphabetically.pdf", b"%PDF 1")
    pdf(root, "Topic-reading/Unnumbered.pdf", b"%PDF other")
    result = scan(root)
    apply_scan(result, db, data_dir)
    papers = db.list_papers(collection=result.collections[0]["id"], sort="rank")
    assert [p["title"] for p in papers] == ["ZLastAlphabetically", "FirstAlphabetically", "Unnumbered"]


def test_failed_twin_keeps_reading_data(tmp_path, db, data_dir, pipeline_factory, monkeypatch):
    source = pdf(tmp_path / "source", "Local.pdf")
    result = scan(source.parent)
    apply_scan(result, db, data_dir)
    old_id = db.list_papers()[0]["id"]
    db.execute("UPDATE papers SET status='failed' WHERE id=?", (old_id,))
    db.execute("INSERT INTO notes(paper_id,body,source,created_at,updated_at) VALUES(?,'My reading reason','human','','')", (old_id,))
    pipeline = pipeline_factory()
    monkeypatch.setattr(pipeline, "_download", lambda url, dest: dest.write_bytes(source.read_bytes()))
    new_id = pipeline.process("arxiv:2401.12345")
    assert new_id == old_id and len(db.list_papers()) == 1  # reuse the canonical record and its paid checkpoints
    assert len(db.collections(new_id)) == 1
    assert db.notes_for(new_id)[0]["body"] == "My reading reason"
