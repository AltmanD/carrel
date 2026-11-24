"""End-to-end pipeline tests using fake services (no network, no model calls)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carrel.models import CORE_SECTIONS
from carrel.parse.base import ParseLowQuality, ParseRemoteFailed
from carrel.pipeline import PipelineError
from carrel.store.paths import paper_dir, read_json

from .conftest import FakeBackend, FakeParser, FakeRelations, FailingRelations


def run(pipeline, tmp_path: Path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    return pipeline.process(str(pdf))


class TestHappyPath:
    def test_full_pipeline_reaches_ready(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        row = db.get_paper(pid)
        assert row["status"] == "ready", row
        base = paper_dir(db.path.parent, pid)
        for name in ("doc.json", "sections.json", "summary.json",
                     "classification.json", "relations.json", "notes.md"):
            assert (base / name).is_file(), name
        assert (base / "modules").glob("*.json")
        assert (base / "translation").glob("*.json")

    def test_doc_covers_all_pages(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        doc = read_json(paper_dir(db.path.parent, pid) / "doc.json")
        pages = {b["page"] for b in doc["blocks"]}
        assert doc["pages"] == max(pages)
        assert len(pages) == doc["pages"]

    def test_modules_are_separate_files(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        mods = {p.stem for p in (paper_dir(db.path.parent, pid) / "modules").glob("*.json")}
        assert "abstract" in mods
        assert "method" in mods
        assert "references" not in mods, "references should not be extracted"

    def test_summary_digests_match_modules(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        base = paper_dir(db.path.parent, pid)
        summary = read_json(base / "summary.json")
        mods = [read_json(p) for p in (base / "modules").glob("*.json")]
        by_kind = {m["kind"]: m["digest"] for m in mods}
        for digest in summary["section_digests"]:
            assert by_kind[digest["kind"]] == digest["text"]

    def test_translation_written_per_module(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        tr = paper_dir(db.path.parent, pid) / "translation"
        files = [p for p in tr.glob("*.json") if not p.name.startswith("_")]
        assert files
        payload = read_json(files[0])
        assert "translations" in payload
        assert payload["qa"]["ok"] is True

    def test_tags_persisted_with_block_ids(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        tags = db.tags_for(pid)
        assert tags
        assert all(t["source_block"] for t in tags)

    def test_relations_recorded_in_three_kinds(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        kinds = {r["kind"] for r in db.relations_for(pid)}
        assert kinds == {"cites", "cited_by", "similar"}

    def test_glossary_saved(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        gloss = read_json(paper_dir(db.path.parent, pid) / "glossary.json")
        assert "terms" in gloss

    def test_assessment_is_separate_from_summary(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        base = paper_dir(db.path.parent, pid)
        assess = read_json(base / "assessment.json")
        summary = read_json(base / "summary.json")
        assert assess["origin"] == "llm"
        assert "value" not in summary
        assert assess["run_id"]

    def test_model_runs_recorded(self, pipeline_factory, tmp_path, db):
        pid = run(pipeline_factory(), tmp_path)
        runs = db.runs_for(pid)
        assert any(r["task"] == "extract" for r in runs)
        assert any(r["task"] == "translate" for r in runs)
        assert any(r["task"] == "classify" for r in runs)


class TestIdempotency:
    def test_reimport_does_not_rerun(self, pipeline_factory, tmp_path, db):
        pipeline = pipeline_factory()
        pid = run(pipeline, tmp_path)
        before = len(db.runs_for(pid))
        run(pipeline, tmp_path)
        assert len(db.runs_for(pid)) == before

    def test_same_arxiv_different_link_same_id(self, pipeline_factory, db):
        p1 = pipeline_factory()
        a = p1.process("arxiv:2501.12948")
        b = p1.process("https://arxiv.org/abs/2501.12948")
        assert a == b

    def test_duplicate_pdf_rejected(self, pipeline_factory, tmp_path):
        pipeline = pipeline_factory()
        first = tmp_path / "one.pdf"
        first.write_bytes(b"%PDF-1.4 identical bytes")
        pid1 = pipeline.process(str(first))

        # Same bytes under a different path would hash to the same paper id, so
        # the pipeline should return the existing record rather than fork.
        second = tmp_path / "two.pdf"
        second.write_bytes(b"%PDF-1.4 identical bytes")
        pid2 = pipeline.process(str(second))
        assert pid1 == pid2

    def test_local_pdf_reuses_arxiv_record(self, pipeline_factory, tmp_path, db):
        """Downloading a paper then importing the file must not create a second row."""
        pipeline = pipeline_factory()
        arxiv_id = pipeline.process("arxiv:2501.12948")
        # Write the bytes the arXiv import stored, then import the same file.
        stored = paper_dir(db.path.parent, arxiv_id) / "source.pdf"
        copy = tmp_path / "downloaded.pdf"
        copy.write_bytes(stored.read_bytes())

        resolved = pipeline.process(str(copy))
        assert resolved == arxiv_id
        assert len(db.list_papers()) == 1

    def test_healthy_twin_returned_and_no_junk_row(self, pipeline_factory, tmp_path, db):
        """A second import of bytes we already hold must not leave a failed twin."""
        pipeline = pipeline_factory()
        first = pipeline.process("arxiv:2501.12948")

        # Re-import with force so the early return is bypassed; the parse stage
        # detects the twin mid-flight and must hand back the existing record.
        second = pipeline.process("arxiv:2501.12948", force=True)
        assert second == first
        rows = db.list_papers()
        assert len(rows) == 1
        assert rows[0]["status"] == "ready"

    def test_failed_twin_is_cleaned_not_blocking(self, pipeline_factory, tmp_path, db):
        from carrel.store.paths import read_json

        pipeline = pipeline_factory()
        # First import under one id fails at the parse stage.
        p1 = pipeline_factory(
            parser=FakeParser(raises=ParseRemoteFailed("mineru down"))
        )
        try:
            p1.process("arxiv:2501.12948")
        except PipelineError:
            pass
        assert db.list_papers()[0]["status"] == "failed"

        # Re-import with a healthy parser: the failed twin must not block it.
        pid = pipeline.process("arxiv:2501.12948", force=True)
        assert db.get_paper(pid)["status"] == "ready"
        # No failed twin lingers under a different id.
        assert len(db.list_papers()) == 1


class TestTranslationQuality:
    def test_bad_translation_is_flagged_for_review(self, pipeline_factory, tmp_path, db):
        pipeline = pipeline_factory(backend=FakeBackend(bad_translation=True))
        pid = run(pipeline, tmp_path)
        base = paper_dir(db.path.parent, pid)
        flagged = []
        for path in (base / "translation").glob("*.json"):
            if path.name.startswith("_"):
                continue
            data = read_json(path)
            if data.get("needs_review"):
                flagged.append(data)
        assert flagged, "tampered translation must be flagged"
        assert any(not d["qa"]["ok"] for d in flagged)

    def test_repair_rounds_are_recorded(self, pipeline_factory, tmp_path, db):
        pipeline = pipeline_factory(backend=FakeBackend(bad_translation=True))
        pid = run(pipeline, tmp_path)
        repairs = [r for r in db.runs_for(pid) if r["task"] == "repair"]
        assert repairs, "a failing QA pass must trigger repair runs"
        assert any(r["qa_ok"] == 0 for r in db.runs_for(pid))


class TestFailureModes:
    def test_parse_failure_marks_failed_with_code(self, pipeline_factory, tmp_path, db):
        pipeline = pipeline_factory(
            parser=FakeParser(raises=ParseRemoteFailed("mineru down"))
        )
        with pytest.raises(PipelineError) as exc:
            run(pipeline, tmp_path)
        assert exc.value.code == "parse_remote_failed"
        failed = [p for p in db.list_papers() if p["status"] == "failed"]
        assert len(failed) == 1

    def test_low_quality_parse_does_not_produce_empty_note(self, pipeline_factory,
                                                           tmp_path, db):
        pipeline = pipeline_factory(
            parser=FakeParser(raises=ParseLowQuality("only 2 blocks"))
        )
        with pytest.raises(PipelineError):
            run(pipeline, tmp_path)
        pid = db.list_papers()[0]["id"]
        base = paper_dir(db.path.parent, pid)
        assert not (base / "summary.json").is_file()
        assert not (base / "notes.md").is_file()
        assert read_json(base / "error.json")["code"] == "parse_low_quality"

    def test_relation_failure_downgrades_but_keeps_paper(self, pipeline_factory,
                                                        tmp_path, db):
        pipeline = pipeline_factory(relations=FailingRelations())
        pid = run(pipeline, tmp_path)
        assert db.get_paper(pid)["status"] == "ready_with_warning"

    def test_model_failure_during_extract_fails_paper(self, pipeline_factory,
                                                      tmp_path, db):
        pipeline = pipeline_factory(backend=FakeBackend(fail=True))
        with pytest.raises(PipelineError) as exc:
            run(pipeline, tmp_path)
        assert exc.value.code == "extract_failed"

    def test_missing_local_file_is_reported(self, pipeline_factory):
        pipeline = pipeline_factory()
        # A path that does not exist cannot even be classified, so this is
        # rejected before any stage runs.
        from carrel.ids import UnsupportedInput

        with pytest.raises(UnsupportedInput):
            pipeline.process("/definitely/not/here.pdf")


class TestClassification:
    def test_out_of_taxonomy_creates_proposal(self, pipeline_factory, tmp_path, db):
        backend = FakeBackend()

        original = backend._classification

        def out_of_taxonomy():
            data = original()
            data["categories"] = []
            data["proposal"] = {"discipline": "炼金术", "field": "点石成金",
                                "reason": "论文讨论转化"}
            return data

        backend._classification = out_of_taxonomy
        pid = run(pipeline_factory(backend=backend), tmp_path)
        row = db.get_paper(pid)
        assert row["discipline"] == "未分类"
        assert db.list_proposals(), "an out-of-taxonomy choice must file a proposal"
        assert len(db.category_tree()) > 0

    def test_multi_category_persisted(self, pipeline_factory, tmp_path, db):
        """A paper can belong to several specific subfields."""
        from carrel.store.paths import paper_dir, read_json

        pid = run(pipeline_factory(), tmp_path)
        clf = read_json(paper_dir(db.path.parent, pid) / "classification.json")
        cats = clf.get("categories") or []
        assert len(cats) >= 2, cats
        assert all(c.get("field") and c.get('category_id') and 'subfield' not in c for c in cats)
        assert clf['primary_category_id'] in {c['category_id'] for c in cats}
        # The papers table keeps the primary for faceting.
        row = db.get_paper(pid)
        assert row["discipline"] == cats[0]["discipline"]
        assert row["field"] == cats[0]["field"]

    def test_long_tags_are_dropped(self, pipeline_factory, tmp_path, db):
        backend = FakeBackend()
        original = backend._classification

        def long_tags():
            data = original()
            data["tags"] = [
                {"tag": "这是一个非常非常长的描述性标签不该被接受",
                 "evidence": "x", "block_id": "p1-b3"},
                {"tag": "稠密检索", "evidence": "dense retrieval", "block_id": "p1-b2"},
            ]
            return data

        backend._classification = long_tags
        pid = run(pipeline_factory(backend=backend), tmp_path)
        labels = [t["tag"] for t in db.tags_for(pid)]
        assert "稠密检索" in labels
        assert all(len(t) <= 12 for t in labels), labels

    def test_taxonomy_node_count_unchanged(self, pipeline_factory, tmp_path, db):
        # The pipeline syncs the shipped taxonomy into the DB on construction;
        # classification itself must not add nodes.
        pipeline = pipeline_factory()
        before = len(db.query("SELECT * FROM categories"))
        run(pipeline, tmp_path)
        after = len(db.query("SELECT * FROM categories"))
        assert before == after
        assert before > 0
