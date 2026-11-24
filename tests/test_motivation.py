"""Optional motivation extraction, grounding, invalidation and paid-call backoff."""
import json

import pytest

from carrel.model import ModelError, ModelReply
from carrel.models import Block, DocModel, PaperMeta
from carrel.motivation.extract import (ensure_motivation, extract_motivation,
                                      normalize_motivation, reusable_motivation)
from carrel.store.paths import atomic_write_json, paper_dir, read_json
from tests.conftest import FakeBackend


def seed_motivation_source(config, db, pid="arxiv:2401.00001", **extra):
    paper = PaperMeta(id=pid, title=f"Research {pid}", status="ready", **extra).to_dict()
    db.upsert_paper(paper)
    base = paper_dir(config.data_dir, pid)
    doc = DocModel(backend="fake", pages=1, blocks=[
        Block(id="p1-b1", page=1, type="text", text="Earlier work depends on hand-designed curricula.")])
    atomic_write_json(base / "doc.json", doc.to_dict())
    import hashlib
    fp = doc.fingerprint()
    digest = 'Automate hand-designed curricula'
    extracts_fp = hashlib.sha256(f'introduction\x1fintroduction\x1f{digest}\n'.encode()).hexdigest()[:16]
    atomic_write_json(base / 'sections.json', {'sections': [{'id': 'intro', 'kind': 'introduction',
                      'title': 'Introduction', 'block_ids': ['p1-b1']}]})
    atomic_write_json(base / 'classification.json', {'doc_fingerprint': fp, 'discipline': 'Computer Science',
                      'categories': [{'discipline': 'Computer Science', 'field': 'Machine Learning'}]})
    atomic_write_json(base / "summary.json", {"one_line": "Automate curricula",
                      'doc_fingerprint': fp, 'extracts_fingerprint': extracts_fp,
                      "argument_chain": ["Self-play learns a curriculum"], "limitations": []})
    atomic_write_json(base / "modules" / "introduction.json", {"kind": "introduction",
                      'doc_fingerprint': fp, 'title_original': 'Introduction', 'block_span': ['p1-b1', 'p1-b1'],
                      "digest": "Automate hand-designed curricula",
                      "points": [{"text": "Manual curriculum", "block_id": "p1-b1"}]})
    return paper, base


class MotivationBackend(FakeBackend):
    def __init__(self, payload=None, fail_task=""):
        super().__init__()
        self.payload = payload
        self.fail_task = fail_task
        self.attempts = 0

    def structured(self, **kwargs):
        if kwargs.get("task") == self.fail_task:
            self.attempts += 1
            raise ModelError("simulated motivation outage")
        if kwargs.get("task") == "motivation" and self.payload is not None:
            self.calls.append({"task": "motivation"})
            return ModelReply(text=json.dumps(self.payload), data=self.payload, tokens_in=12, tokens_out=10)
        return super().structured(**kwargs)


def test_normalize_aliases_and_wrapper():
    data = normalize_motivation([{"value": {"研究动机": "减少人工课程", "研究空白": "依赖手工",
                  "研究方向": "智能体", "关键词": "自博弈，课程;智能体", "证据块": "p1-b1;p2-b3"}}])
    assert data["motivation"] == "减少人工课程"
    assert data["keywords"] == ["自博弈", "课程", "智能体"]
    assert data["evidence_block_ids"] == ["p1-b1", "p2-b3"]
    assert "problem" not in data
    assert normalize_motivation({"gap": "missing"}) is None


def test_evidence_intersection_and_digest_only():
    backend = MotivationBackend({"motivation": "M", "evidence_block_ids": ["p1-b1", "invented"]})
    data, _ = extract_motivation(backend, title="T", summary={},
                                intro_extract={"points": [{"block_id": "p1-b1"}]})
    assert data["evidence_block_ids"] == ["p1-b1"]
    data, _ = extract_motivation(backend, title="T", summary={}, intro_extract={"digest": "D"})
    assert data["evidence_block_ids"] == ["p1-b1", "invented"]


def test_cache_tracks_summary_and_extract_changes(config, db):
    paper, base = seed_motivation_source(config, db)
    backend = FakeBackend()
    first = ensure_motivation(config, db, backend, paper)
    assert ensure_motivation(config, db, backend, paper) == first
    assert len(backend.calls) == 1
    summary = read_json(base / "summary.json")
    summary["one_line"] = "Changed summary, identical original document"
    atomic_write_json(base / "summary.json", summary)
    assert reusable_motivation(base) is None
    second = ensure_motivation(config, db, backend, paper)
    assert first["doc_fingerprint"] == second["doc_fingerprint"]
    assert first["input_fingerprint"] != second["input_fingerprint"]
    intro = read_json(base / "modules" / "introduction.json")
    intro["digest"] += " updated extraction"
    atomic_write_json(base / "modules" / "introduction.json", intro)
    assert ensure_motivation(config, db, backend, paper)["input_fingerprint"] != second["input_fingerprint"]
    assert len(backend.calls) == 3


def test_backoff_and_explicit_retry(config, db):
    paper, base = seed_motivation_source(config, db)
    backend = MotivationBackend(fail_task="motivation")
    for _ in range(4):
        assert ensure_motivation(config, db, backend, paper) is None
    assert backend.attempts == 2
    assert read_json(base / "_motivation_attempts.json")["n"] == 2
    assert [r["status"] for r in db.runs_for(paper["id"])] == ["failed", "failed"]
    assert ensure_motivation(config, db, backend, paper, retry=True) is None
    assert backend.attempts == 3
    backend.fail_task = ""
    assert ensure_motivation(config, db, backend, paper, retry=True)["motivation"]
    assert not (base / "_motivation_attempts.json").exists()


def test_missing_intro_is_skipped_without_model(config, db):
    paper, base = seed_motivation_source(config, db)
    (base / "modules" / "introduction.json").unlink()
    backend = FakeBackend()
    assert ensure_motivation(config, db, backend, paper) is None
    assert ensure_motivation(config, db, backend, paper) is None
    assert not backend.calls
    assert len(db.runs_for(paper["id"])) == 1
    assert db.runs_for(paper["id"])[0]["status"] == "skipped"


def test_digest_only_still_checks_doc_evidence(config, db):
    paper, base = seed_motivation_source(config, db)
    atomic_write_json(base / "modules" / "introduction.json", {"digest": "D"})
    backend = MotivationBackend({"motivation": "M", "evidence_block_ids": ["p1-b1", "invented"]})
    assert ensure_motivation(config, db, backend, paper)["evidence_block_ids"] == ["p1-b1"]


def test_requested_motivation_failure_is_partial_and_retries_only_missing_work(pipeline_factory, sample_pdf, db):
    backend = MotivationBackend(fail_task="motivation")
    pipeline = pipeline_factory(backend=backend)
    pid = pipeline.process(str(sample_pdf))
    assert db.get_paper(pid)["status"] == "partial"
    assert any(r["task"] == "motivation" and r["status"] == "failed" for r in db.runs_for(pid))
    before = len(backend.calls)
    assert pipeline.process(str(sample_pdf)) == pid
    assert len(backend.calls) == before and backend.attempts == 2


def test_classify_failure_still_saves_paid_motivation(pipeline_factory, sample_pdf, db):
    from carrel.pipeline import PipelineError
    backend = MotivationBackend(fail_task="classify")
    pipeline = pipeline_factory(backend=backend)
    with pytest.raises(PipelineError):
        pipeline.process(str(sample_pdf))
    pid = db.list_papers()[0]["id"]
    assert read_json(paper_dir(pipeline.data_dir, pid) / "motivation.json")["motivation"]
    assert any(r["task"] == "motivation" and r["status"] == "ok" for r in db.runs_for(pid))


def test_reused_classify_assess_can_still_fill_motivation(pipeline_factory, sample_pdf, db):
    backend = FakeBackend()
    pipeline = pipeline_factory(backend=backend)
    pid = pipeline.process(str(sample_pdf))
    base = paper_dir(pipeline.data_dir, pid)
    (base / "motivation.json").unlink()
    before = len(backend.calls)
    db.set_status(pid, "partial")
    pipeline.process(str(sample_pdf))
    assert [c["task"] for c in backend.calls[before:]] == ["motivation"]
