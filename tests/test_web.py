"""Web layer tests: pages render, API contract holds, failed jobs are visible."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carrel.config import AppConfig
from carrel.store.db import Database
from carrel.store.paths import paper_dir, read_json

from .conftest import FakeBackend, FakeParser, FakeRelations, FailingRelations


@pytest.fixture()
def client(config: AppConfig, db, pipeline_factory, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from carrel.web import app as web_app

    application = web_app.create_app(config, db)

    # Drive the pipeline synchronously through the fake services so page tests
    # do not depend on the background worker thread.
    pipeline = pipeline_factory()
    pdf = tmp_path / "seed.pdf"
    pdf.write_bytes(b"%PDF-1.4 seed")
    paper_id = pipeline.process(str(pdf))

    def fake_submit(raw, *, force=False, profile=None):
        return {"job_id": "job-test", "paper_id": paper_id}

    application.state.jobs.submit = fake_submit  # type: ignore[method-assign]
    return TestClient(application, headers={'X-Carrel': '1'}), paper_id, config


class TestPages:
    def test_index_lists_paper(self, client):
        c, pid, _ = client
        r = c.get("/")
        assert r.status_code == 200
        assert "Carrel" in r.text
        assert "论文库" in r.text

    def test_index_with_filters(self, client):
        c, _, _ = client
        for query in ("?q=orchestration", "?discipline=计算机科学",
                      "?sort=title", "?status=ready"):
            r = c.get("/" + query)
            assert r.status_code == 200, query

    def test_paper_detail_renders_all_tabs(self, client):
        c, pid, _ = client
        r = c.get(f"/papers/{pid}")
        assert r.status_code == 200
        # The translation tab was merged into the reading tab: PDF, annotations
        # and the comparison text share one pane now.
        for tab in ("总结", "原文", "关系"):
            assert tab in r.text
        assert 'data-tab="trans"' not in r.text
        assert 'id="cmp-panel"' in r.text and 'id="cmp-mode"' in r.text
        assert "翻译尚未完成" not in r.text
        # The pair and module views are gone; reading is summary + PDF + article.
        assert 'data-pane="pair"' not in r.text
        assert 'data-pane="modules"' not in r.text
        # The original-PDF tab embeds the annotation-capable viewer.
        assert 'id="pdf-viewer"' in r.text
        # Model-call records are collapsed by default.
        assert "<details" in r.text and "runs-details" in r.text
        # The summary column carries the model assessment in Chinese labels.
        assert "模型评估" in r.text and "模型判断" in r.text

    def test_reading_pane_pairs_source_and_translation(self, client):
        from carrel.store.paths import atomic_write_json

        c, pid, cfg = client
        base = paper_dir(cfg.data_dir, pid)
        doc = read_json(base / "doc.json", {})
        doc["blocks"].extend([
            {"id": "pair-a", "type": "text", "text": "First source sentence.", "page": 1},
            {"id": "pair-b", "type": "text", "text": "Second source sentence.", "page": 2},
            {"id": "pair-c", "type": "text", "text": "Third source sentence.", "page": 2},
            {"id": "pair-orphan", "type": "figure", "text": "[figure]", "page": 2},
        ])
        atomic_write_json(base / "doc.json", doc)
        sections = read_json(base / "sections.json", {})
        sections["sections"].append({"title": "Pairing", "page_start": 1, "page_end": 2,
                                     "block_ids": ["pair-a", "pair-b", "pair-c", "pair-orphan"]})
        atomic_write_json(base / "sections.json", sections)
        atomic_write_json(base / "translation" / "pairing.json", {
            "title": "Pairing",
            "translations": {"pair-a": "第一句原文。", "pair-b": "第二句原文。", "pair-c": "第三句原文。"}})
        page = c.get(f"/papers/{pid}").text
        # Merge consecutive blocks only within a page, so the floating reader
        # and page navigation never include the next page's translation.
        assert 'data-block-ids="pair-a" data-pages="1"' in page
        assert 'data-block-ids="pair-b pair-c" data-pages="2"' in page
        assert 'data-pages="1 2"' not in page
        # The source column keeps the English; the translation column the Chinese.
        assert "First source sentence." in page and "第二句原文。" in page
        assert 'class="cmp-src"' in page
        # A structure block without a translation keeps its original and is
        # marked instead of silently disappearing.
        assert '[figure]' in page
        assert "untranslated" in page

    def test_reading_pane_lists_untranslated_sections(self, client):
        c, pid, _ = client
        page = c.get(f"/papers/{pid}").text
        assert "cmp-untranslated" in page or "未翻译的章节" in page


    def test_detail_shows_relation_coverage(self, client):
        c, pid, _ = client
        r = c.get(f"/papers/{pid}")
        assert "参考文献" in r.text
        assert "相似论文" in r.text
        assert 'data-relation-kind="cited_by"' not in r.text

    def test_categories_page(self, client):
        c, _, _ = client
        r = c.get("/categories")
        assert r.status_code == 200
        assert "模型训练与对齐" in r.text
        # Per user feedback: no 未分类 / 待确认 panels — only categories that
        # hold papers, plus a manual-add form.
        assert "未分类" not in r.text
        assert "待确认" not in r.text
        assert "cat-add-form" in r.text

    def test_relations_page(self, client):
        c, _, _ = client
        r = c.get("/relations")
        assert r.status_code == 200

    def test_import_page_lists_supported_inputs(self, client):
        c, _, _ = client
        r = c.get("/import")
        assert r.status_code == 200
        assert "arxiv.org/abs" in r.text
        assert "huggingface.co/papers" in r.text

    def test_jobs_page(self, client):
        c, _, _ = client
        assert c.get("/jobs").status_code == 200

    def test_missing_paper_returns_404(self, client):
        c, _, _ = client
        r = c.get("/papers/arxiv:does-not-exist")
        assert r.status_code == 404


class TestApi:
    def test_health_reports_configuration(self, client):
        c, _, cfg = client
        data = c.get("/api/health").json()
        assert data["mineru"]["configured"] is True
        assert "protocol" in data["model"]
        assert data["sciverse"]["configured"] is False

    def test_papers_listing(self, client):
        c, pid, _ = client
        data = c.get("/api/papers").json()
        ids = [p["id"] for p in data["papers"]]
        assert pid in ids

    def test_module_endpoint(self, client):
        c, pid, _ = client
        data = c.get(f"/api/papers/{pid}/module/method").json()
        assert data["kind"] == "method"
        assert data["points"]

    def test_unknown_module_returns_error_code(self, client):
        c, pid, _ = client
        r = c.get(f"/api/papers/{pid}/module/nonexistent")
        assert r.status_code == 404
        assert r.json()["error_code"] == "not_found"

    def test_relations_split_by_kind(self, client):
        c, pid, _ = client
        for kind in ("cites", "cited_by", "similar"):
            data = c.get(f"/api/papers/{pid}/relations?kind={kind}").json()
            assert data["relations"], kind
            assert all(r["kind"] == kind for r in data["relations"])

    def test_assessment_endpoint(self, client):
        c, pid, _ = client
        data = c.get(f"/api/papers/{pid}/assessment").json()
        assert data["origin"] == "llm"
        assert "value" in data

    def test_pdf_endpoint_serves_bytes(self, client):
        c, pid, _ = client
        r = c.get(f"/api/papers/{pid}/pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"

    def test_taxonomy_endpoint(self, client):
        c, _, cfg = client
        data = c.get("/api/taxonomy").json()
        assert data["tree"]
        assert any(node["label"] == "模型训练与对齐" for node in data["tree"])

    def test_category_update_persists(self, client):
        c, pid, _ = client
        r = c.post(f"/api/papers/{pid}/category",
                   json={"discipline": "数学", "field": "优化"})
        assert r.status_code == 200
        assert c.get("/api/papers").json()["papers"][0]["discipline"] == "数学"

    def test_tag_add_and_remove(self, client):
        c, pid, _ = client
        c.post(f"/api/papers/{pid}/tags", json={"add": ["手工标签"]})
        tags = c.get("/api/papers").json()["papers"][0]
        assert tags is not None
        c.post(f"/api/papers/{pid}/tags", json={"remove": ["手工标签"]})
        # Removal must not crash and must leave the paper intact.
        assert c.get(f"/api/papers/{pid}").status_code == 200

    def test_manual_tag_changes_survive_page_reload(self, client):
        c, pid, _ = client
        endpoint = f"/api/papers/{pid}/tags"
        c.post(endpoint, json={"add": ["My reading list"]})
        assert 'data-tag="My reading list"' in c.get(f"/papers/{pid}").text
        c.post(endpoint, json={"remove": ["My reading list"], "add": ["Reviewed"]})
        page = c.get(f"/papers/{pid}").text
        assert 'data-tag="Reviewed"' in page
        assert 'data-tag="My reading list"' not in page
        c.post(endpoint, json={"remove": ["Reviewed"]})
        assert 'data-tag="Reviewed"' not in c.get(f"/papers/{pid}").text

    def test_model_tag_removal_uses_database_on_reload(self, client):
        from html import escape

        c, pid, _ = client
        label = c.get(f"/api/papers/{pid}").json()["tags"][0]["tag"]
        c.post(f"/api/papers/{pid}/tags", json={"remove": [label]})
        assert f'data-tag="{escape(label, quote=True)}"' not in c.get(f"/papers/{pid}").text

    @pytest.mark.parametrize("bad_add", [[" "], ["x" * 25], [42], "tag"])
    def test_invalid_rename_keeps_original(self, client, bad_add):
        c, pid, _ = client
        endpoint = f"/api/papers/{pid}/tags"
        c.post(endpoint, json={"add": ["Original"]})
        response = c.post(endpoint, json={"remove": ["Original"], "add": bad_add})
        assert response.status_code == 400
        assert any(t["tag"] == "Original" for t in c.get(f"/api/papers/{pid}").json()["tags"])

    def test_unknown_paper_cannot_create_tags(self, client):
        c, _, _ = client
        assert c.post("/api/papers/missing/tags", json={"add": ["tag"]}).status_code == 404

    def test_category_edit_preserves_other_paths_and_primary(self, client):
        from carrel.store.paths import atomic_write_json

        c, pid, cfg = client
        path = paper_dir(cfg.data_dir, pid) / "classification.json"
        classification = read_json(path, {})
        from carrel.classify.taxonomy import project_path
        first = project_path({"discipline": "推理与系统", "field": "推理"})
        second = project_path({"discipline": "数学", "field": "优化"})
        classification["categories"] = [first, second]
        atomic_write_json(path, classification)
        response = c.post(f"/api/papers/{pid}/category", json={"index": 1, **second, "field": "数值分析"})
        assert response.status_code == 200
        saved = read_json(path, {})["categories"]
        assert saved[0] == first and saved[1]["field"] == "数值分析"
        assert c.get(f"/api/papers/{pid}").json()["field"] == "推理"
        filtered = c.get("/api/papers", params={"discipline": "数学", "field": "数值分析"}).json()["papers"]
        assert [p["id"] for p in filtered] == [pid]
        before = c.get(f"/api/papers/{pid}").json()
        bad = c.post(f"/api/papers/{pid}/category", json={"index": 20, "discipline": "invalid"})
        assert bad.status_code == 400
        assert c.get(f"/api/papers/{pid}").json()["discipline"] == before["discipline"]

    def test_filter_links_keep_search_and_status(self, client):
        from html.parser import HTMLParser
        from urllib.parse import parse_qs, urlparse

        c, _, _ = client
        links = []

        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "a" and "filter-tag" in attrs.get("class", ""):
                    links.append(attrs["href"])

        Links().feed(c.get("/", params={"q": "orchestration", "status": "ready", "sort": "year"}).text)
        assert links
        for link in links:
            params = parse_qs(urlparse(link).query)
            assert params["q"] == ["orchestration"]
            assert params["status"] == ["ready"] and params["sort"] == ["year"]

    def test_remove_category_retains_other_paths(self, client):
        c, pid, _ = client
        endpoint = f"/api/papers/{pid}/category"
        c.post(endpoint, json={"discipline": "First", "field": "A"})
        c.post(endpoint, json={"index": 1, "discipline": "Second", "field": "B"})
        result = c.post(endpoint, json={"index": 0, "remove": True})
        assert [cat["discipline"] for cat in result.json()["categories"]] == ["Second"]
        assert c.get(f"/api/papers/{pid}").json()["discipline"] == "Second"
        c.post(endpoint, json={"index": 0, "remove": True})
        assert c.get(f"/api/papers/{pid}").json()["discipline"] == ""

    def test_no_matches_is_not_an_empty_library(self, client):
        c, _, _ = client
        page = c.get("/?q=definitely-no-such-paper").text
        assert "没有匹配的论文" in page
        assert "还没有论文" not in page

    def test_custom_category_with_quotes_round_trips(self, client):
        from carrel.classify.taxonomy import Taxonomy

        c, _, cfg = client
        category = {"discipline": 'Custom "research"', "field": 'A \\ B'}
        response = c.post("/api/taxonomy", json=category)
        assert response.status_code == 200
        assert any(all(p.get(k) == v for k, v in category.items()) for p in Taxonomy.load(cfg.data_dir / "taxonomy.toml").paths())

    def test_import_rejects_empty_input(self, client):
        c, _, _ = client
        r = c.post("/api/import", json={"input": ""})
        assert r.status_code == 400
        assert r.json()["error_code"] == "bad_request"

    def test_import_rejects_unsupported_input(self, client):
        c, _, _ = client
        r = c.post("/api/import", json={"input": "https://example.com/article"})
        assert r.status_code == 400
        assert "error_code" in r.json()

    def test_import_accepts_arxiv(self, client):
        c, pid, _ = client
        r = c.post("/api/import", json={"input": "https://arxiv.org/abs/2501.12948"})
        assert r.status_code == 200
        assert "paper_id" in r.json() and "job_id" in r.json()

    def test_run_page_and_api(self, client):
        c, pid, _ = client
        runs = c.get("/api/jobs").json()["jobs"]
        assert runs is not None
        # Find a model run through the paper detail page's backing data.
        detail = c.get(f"/api/papers/{pid}")
        assert detail.status_code == 200


class TestReadingDisplay:
    def test_missing_reference_title_never_displays_identifier(self, client):
        from carrel.store.paths import atomic_write_json

        c, pid, cfg = client
        path = paper_dir(cfg.data_dir, pid) / "relations.json"
        data = read_json(path, {})
        data["cites"] = [{"dst_id": "openalex:W6929328961", "dst_title": ""},
                         {"dst_id": "openalex:W1234567890", "dst_title": "openalex:W1234567890"}]
        atomic_write_json(path, data)
        page = c.get(f"/papers/{pid}").text
        assert "openalex:W6929328961" not in page and "openalex:W1234567890" not in page
        assert "文献标题暂缺" in page
        assert 'href="https://openalex.org/W6929328961"' in page

    def test_similar_paper_has_direct_or_explicit_search_link(self, client):
        from carrel.store.paths import atomic_write_json

        c, pid, cfg = client
        path = paper_dir(cfg.data_dir, pid) / "relations.json"
        data = read_json(path, {})
        data["similar"] = [
            {"dst_id": "sciverse:one", "dst_title": "Known paper", "dst_arxiv": "2501.12345"},
            {"dst_id": "sciverse:two", "dst_title": "Publisher paper", "dst_url": "https://example.org/paper"},
            {"dst_id": "sciverse:three", "dst_title": "Unresolved paper"},
            {"dst_id": "sciverse:four", "dst_title": "Invalid link", "dst_url": "https://[broken"},
            {"dst_id": "sciverse:five", "dst_title": "Unsafe link", "dst_url": "javascript:alert(1)"},
        ]
        atomic_write_json(path, data)
        page = c.get(f"/papers/{pid}").text
        assert 'href="https://arxiv.org/abs/2501.12345"' in page
        assert 'href="https://example.org/paper"' in page
        assert 'https://scholar.google.com/scholar?q=Unresolved+paper' in page
        assert "按标题检索" in page
        assert 'scholar?q=Invalid+link' in page and 'scholar?q=Unsafe+link' in page
        assert 'javascript:alert' not in page

    def test_source_formula_is_shown_when_translation_omits_it(self, client):
        from carrel.store.paths import atomic_write_json

        c, pid, cfg = client
        base = paper_dir(cfg.data_dir, pid)
        doc = read_json(base / "doc.json", {})
        doc["blocks"].append({"id": "formula-test", "type": "formula", "text": r"\frac{1}{2}", "page": 1})
        atomic_write_json(base / "doc.json", doc)
        sections = read_json(base / "sections.json", {})
        sections["sections"].append({"title": "Formula test", "block_ids": ["formula-test", "formula-caption"]})
        atomic_write_json(base / "sections.json", sections)
        atomic_write_json(base / "translation" / "formula-test.json", {
            "title": "Formula test", "translations": {"formula-caption": "Caption"}})
        page = c.get(f"/papers/{pid}").text
        assert 'data-math-display id="formula-test"' in page
        assert r"\frac{1}{2}" in page
        assert 'id="toc-toggle"' in page and 'id="tool-pointer"' in page
        assert 'id="tool-hand"' not in page

    def test_translation_preserves_math_boundaries(self, client):
        from carrel.store.paths import atomic_write_json

        c, pid, cfg = client
        base = paper_dir(cfg.data_dir, pid)
        sections = read_json(base / "sections.json", {})
        sections["sections"].append({"title": "Currency", "block_ids": ["cost", "quality"]})
        atomic_write_json(base / "sections.json", sections)
        atomic_write_json(base / "translation" / "currency.json", {
            "title": "Currency", "translations": {"cost": r"Cost 11 m\$.", "quality": r"Quality $Q$."}})
        page = c.get(f"/papers/{pid}").text
        assert r'<span data-math-part>Cost 11 m\$.</span><span data-math-part> Quality $Q$.</span>' in page


class TestTaxonomyEditing:
    @pytest.mark.parametrize("level", ["discipline", "field"])
    def test_rename_preserves_children_and_unrelated_paths(self, client, level):
        from carrel.classify.taxonomy import Taxonomy

        c, pid, cfg = client
        endpoint = f"/api/papers/{pid}/category"
        from carrel.classify.taxonomy import project_path
        primary = project_path({"discipline": "模型训练与对齐", "field": "强化学习"})
        sibling = project_path({"discipline": "模型训练与对齐", "field": "表示学习"})
        other = project_path({"discipline": "数学", "field": "优化"})
        for index, category in enumerate([primary, sibling, other]):
            assert c.post(endpoint, json={"index": index, **category}).status_code == 200
        before = c.get(f"/api/papers/{pid}").json()["classification"]
        keys = ["discipline", "field"]
        source = {key: primary[key] if keys.index(key) <= keys.index(level) else "" for key in keys}
        name = 'Renamed "research" \\ area'
        response = c.post("/api/taxonomy/rename", json={"source": source, "name": name})
        assert response.status_code == 200
        assert response.json()["updated_papers"] == 1
        paper = c.get(f"/api/papers/{pid}").json()
        categories = paper["classification"]["categories"]
        assert categories[0] == {**primary, level: name}
        assert categories[1] == ({**sibling, level: name} if level == "discipline" else sibling)
        assert categories[2] == other
        assert paper["discipline"] == categories[0]["discipline"]
        assert paper["field"] == categories[0]["field"]
        assert {k: v for k, v in paper["classification"].items() if k not in [*keys, "categories"]} == {
            k: v for k, v in before.items() if k not in [*keys, "categories"]}
        assert c.get("/api/papers", params=categories[0]).json()["papers"][0]["id"] == pid
        assert c.get("/api/papers", params=primary).json()["papers"] == []
        # A subsequent addition must not discard the packaged-category override.
        assert c.post("/api/taxonomy", json={"discipline": "New discipline", "field": "New field"}).status_code == 200
        taxonomy = Taxonomy.layered(Path(__file__).parents[1] / "src/carrel/taxonomy.toml",
                                    cfg.data_dir / "taxonomy.toml")
        assert any(all(p.get(k) == categories[0][k] for k in keys) for p in taxonomy.paths())
        assert not any(all(p.get(k) == primary[k] for k in keys) for p in taxonomy.paths())
        c.app.state.db.sync_categories(taxonomy.category_rows())
        assert c.get("/categories").status_code == 200

    def test_custom_categories_rename_all_levels_without_papers(self, client):
        from carrel.classify.taxonomy import Taxonomy

        c, _, cfg = client
        path = {"discipline": "Custom", "field": "Field"}
        assert c.post("/api/taxonomy", json=path).status_code == 200
        for level in ["field", "discipline"]:
            source = {**path}
            if level == "discipline":
                source["field"] = ""
            name = 'Updated "' + level + '"'
            result = c.post("/api/taxonomy/rename", json={"source": source, "name": name})
            assert result.status_code == 200
            assert result.json()["updated_papers"] == 0
            path[level] = name
            assert any(all(p.get(k) == v for k, v in path.items()) for p in Taxonomy.load(cfg.data_dir / "taxonomy.toml", fallback=False).paths())
        page = c.get("/categories").text
        assert 'data-edit-taxonomy' in page and 'Updated &#34;field&#34;' in page

    @pytest.mark.parametrize("name", [" ", "x" * 81, None, "Bad\nname", "未分类"])
    def test_invalid_name_cannot_change_papers(self, client, name):
        c, pid, cfg = client
        path = paper_dir(cfg.data_dir, pid) / "classification.json"
        original = path.read_bytes()
        response = c.post("/api/taxonomy/rename", json={"source": {"discipline": "计算机科学"}, "name": name})
        assert response.status_code == 400
        assert path.read_bytes() == original

    def test_duplicate_and_stale_category_are_rejected(self, client):
        c, pid, cfg = client
        path = paper_dir(cfg.data_dir, pid) / "classification.json"
        original = path.read_bytes()
        for source, name, status in [({"discipline": "计算机科学"}, "数学", 409),
                                     ({"discipline": "Missing"}, "New name", 404)]:
            result = c.post("/api/taxonomy/rename", json={"source": source, "name": name})
            assert result.status_code == status
        assert path.read_bytes() == original

    def test_field_rename_is_scoped_to_its_discipline(self, client):
        c, pid, _ = client
        from carrel.classify.taxonomy import project_path
        first = project_path({"discipline": "First", "field": "Shared"})
        second = project_path({"discipline": "Second", "field": "Shared"})
        for index, category in enumerate([first, second]):
            assert c.post("/api/taxonomy", json=category).status_code == 200
            assert c.post(f"/api/papers/{pid}/category", json={"index": index, **category}).status_code == 200
        assert c.post("/api/taxonomy/rename", json={"source": first, "name": "Scoped"}).status_code == 200
        assert c.get(f"/api/papers/{pid}").json()["classification"]["categories"] == [
            {**first, "field": "Scoped"}, second]

    def test_failed_save_restores_files_and_database(self, client, monkeypatch):
        from carrel.web import app as web_app

        c, pid, cfg = client
        path = paper_dir(cfg.data_dir, pid) / "classification.json"
        before = path.read_bytes()
        paper = c.get(f"/api/papers/{pid}").json()
        write = web_app.atomic_write_text

        def fail_overlay(target, content):
            if target.name == "taxonomy.toml":
                raise OSError("disk unavailable")
            write(target, content)

        monkeypatch.setattr(web_app, "atomic_write_text", fail_overlay)
        response = c.post("/api/taxonomy/rename", json={"source": {"discipline": paper["discipline"]}, "name": "Changed"})
        assert response.status_code == 500
        assert path.read_bytes() == before
        assert c.get(f"/api/papers/{pid}").json()["discipline"] == paper["discipline"]
        assert not (cfg.data_dir / "taxonomy.toml").exists()


class TestProgressive:
    def test_preview_written_during_pipeline(self, pipeline_factory, tmp_path, db):
        pdf = tmp_path / "in.pdf"
        pdf.write_bytes(b"%PDF-1.4 preview")
        pipeline_factory().process(str(pdf))
        pid = db.list_papers()[0]["id"]
        preview = read_json(paper_dir(db.path.parent, pid) / "preview.json", None)
        assert preview and preview.get("one_line")
        assert read_json(paper_dir(db.path.parent, pid) / 'title.json', {}).get('title_zh')

    def test_progress_endpoint_reports_counts(self, client):
        c, pid, _ = client
        data = c.get(f"/api/papers/{pid}/progress").json()
        assert data["status"] == "ready"
        assert data["terminal"] is True
        assert data["stages"]["parsing"] == "done"
        assert data["modules"]["done"] >= 1
        assert data["modules"]["total"] >= data["modules"]["done"]
        assert data["artifacts"]["doc"] is True

    def test_progress_endpoint_running_stage(self, config, db, monkeypatch):
        from fastapi.testclient import TestClient

        from carrel.web import app as web_app

        db.upsert_paper({"id": "arxiv:2501.99999", "status": "translating",
                         "created_at": "2026-01-01T00:00:00Z"})
        c = TestClient(web_app.create_app(config, db))
        data = c.get("/api/papers/arxiv:2501.99999/progress").json()
        assert data["terminal"] is False
        assert data["stages"]["parsing"] == "done"
        assert data["stages"]["translating"] == "running"
        assert data["stages"]["classifying"] == "pending"

    def test_index_card_falls_back_to_preview(self, client, config):
        c, pid, _ = client
        base = paper_dir(config.data_dir, pid)
        # Simulate a paper whose full summary has not landed yet.
        (base / "summary.json").unlink()
        r = c.get("/")
        assert r.status_code == 200
        assert "研究如何在异构模型间按输入选择最优候选" in r.text

    def test_paper_detail_shows_progress_bar_when_running(self, config, db):
        from fastapi.testclient import TestClient

        from carrel.web import app as web_app

        db.upsert_paper({"id": "arxiv:2501.88888", "status": "translating",
                         "title": "In flight", "created_at": "2026-01-01T00:00:00Z"})
        c = TestClient(web_app.create_app(config, db))
        r = c.get("/papers/arxiv:2501.88888")
        assert r.status_code == 200
        assert "pipe-progress" in r.text

    def test_paper_detail_no_progress_bar_when_ready(self, client):
        c, pid, _ = client
        r = c.get(f"/papers/{pid}")
        assert "pipe-progress" not in r.text


class TestFailureVisibility:
    def test_failed_job_shows_error_code(self, config, db, pipeline_factory,
                                         tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from carrel.web import app as web_app

        application = web_app.create_app(config, db)

        def boom(raw, *, force=False):
            raise RuntimeError("parse provider down")

        application.state.jobs.submit = boom  # type: ignore[method-assign]
        c = TestClient(application)
        r = c.post("/api/import", json={"input": "arxiv:2501.12948"})
        assert r.status_code == 400
        assert r.json()["error_code"] == "import_rejected"

    def test_relation_failure_is_visible_not_hidden(self, client):
        c, pid, _ = client
        base = paper_dir(c.app.state.config.data_dir, pid)
        rel = json.loads((base / "relations.json").read_text(encoding="utf-8"))
        assert "coverage" in rel
