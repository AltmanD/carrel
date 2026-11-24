"""Budget failures must be recoverable without losing paid work."""

from __future__ import annotations

import json
import re

import pytest

from carrel.model import ModelReply, ModelTruncated
from carrel.pipeline import PipelineError, _split_for_translation
from carrel.store.paths import paper_dir
from tests.conftest import FakeBackend


def _import(pipeline, tmp_path, **kwargs):
    source = tmp_path / "recovery.pdf"
    if not source.exists():
        source.write_bytes(b"%PDF-1.4 recovery")
    return pipeline.process(str(source), **kwargs)


def _truncated(task):
    return ModelTruncated("output limit", ModelReply(
        text='{"unfinished":', tokens_in=100, tokens_out=12000,
        stop_reason="max_tokens", max_tokens=12000, cost_usd=0.01, task=task))


class TruncatesOnce(FakeBackend):
    def __init__(self, task):
        super().__init__()
        self.task = task
        self.failed = False
        self.requests = []

    def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
        if task == self.task:
            ids = re.findall(r"^\[(p\d+-b\d+)\]", user, re.M)
            self.requests.append((ids, user))
            if not self.failed and len(ids) > 1:
                self.failed = True
                raise _truncated(task)
        return super().structured(schema=schema, system=system, user=user,
                                  max_tokens=max_tokens, effort=effort, task=task)


def test_truncated_translation_splits_and_books_every_attempt(pipeline_factory, tmp_path, db):
    backend = TruncatesOnce("translate")
    pipeline = pipeline_factory(backend=backend)
    pid = _import(pipeline, tmp_path)
    assert backend.failed
    assert db.get_paper(pid)["status"] == "ready"
    runs = [r for r in db.runs_for(pid) if r["task"] == "translate"]
    assert len(runs) == len(backend.requests)
    assert sum(r["truncated"] for r in runs) == 1
    assert sum(r["cost_usd"] for r in runs) == pytest.approx(0.01 + 0.0001 * (len(runs) - 1))
    original = set(backend.requests[0][0])
    assert all(set(ids) != original for ids, _ in backend.requests[1:])
    assert not list((paper_dir(pipeline.data_dir, pid) / "translation" / "_checkpoints").glob("*.json"))


def test_extract_truncation_gets_one_compact_retry(pipeline_factory, tmp_path, db):
    backend = TruncatesOnce("extract")
    pipeline = pipeline_factory(backend=backend)
    pid = _import(pipeline, tmp_path)
    assert backend.failed and db.get_paper(pid)["status"] == "ready"
    retries = [prompt for _, prompt in backend.requests if "上次回复超出输出预算" in prompt]
    assert len(retries) == 1
    runs = [r for r in db.runs_for(pid) if r["task"] == "extract"]
    assert len(runs) == len(backend.requests)
    assert sum(r["truncated"] for r in runs) == 1


def test_checkpoint_resume_only_requests_missing_blocks(pipeline_factory, tmp_path, db, monkeypatch):
    from tests.test_resume import FailOnceBackend

    monkeypatch.setattr("carrel.pipeline.MAX_TRANSLATE_CHARS", 160)
    backend = FailOnceBackend(fail_module_text="We evaluate on GAIA")
    pipeline = pipeline_factory(backend=backend)
    pid = _import(pipeline, tmp_path)
    base = paper_dir(pipeline.data_dir, pid)
    checkpoint = base / "translation" / "_checkpoints" / "experiments.json"
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["translations"]
    assert db.get_paper(pid)["status"] == "partial"
    before = sum(c["task"] == "translate" for c in backend.calls)
    _import(pipeline, tmp_path)
    assert sum(c["task"] == "translate" for c in backend.calls) == before + 1
    assert db.get_paper(pid)["status"] == "ready"
    assert not checkpoint.exists()
    final = json.loads((base / "translation" / "experiments.json").read_text(encoding="utf-8"))
    assert saved["translations"].items() <= final["translations"].items()
    assert final["qa"]["checked"] == len(final["translations"])


def test_later_slice_qa_is_not_lost(pipeline_factory, tmp_path, db, monkeypatch):
    monkeypatch.setattr("carrel.pipeline.MAX_TRANSLATE_CHARS", 160)
    pipeline = pipeline_factory(backend=FakeBackend(bad_translation=True))
    pid = _import(pipeline, tmp_path)
    base = paper_dir(pipeline.data_dir, pid)
    saved = json.loads((base / "translation" / "experiments.json").read_text(encoding="utf-8"))
    assert saved["slices"] > 1
    assert saved["qa"]["checked"] == len(saved["translations"])
    assert set(saved["needs_review"]) == {i["block_id"] for i in saved["qa"]["issues"]}
    assert len(saved["needs_review"]) > 1


@pytest.mark.parametrize("field", ["doc_fingerprint", "title", "block_ids"])
def test_stale_checkpoint_is_not_reused(field, pipeline_factory, tmp_path, db, monkeypatch):
    from tests.test_resume import FailOnceBackend

    monkeypatch.setattr("carrel.pipeline.MAX_TRANSLATE_CHARS", 160)
    backend = FailOnceBackend(fail_module_text="We evaluate on GAIA")
    pipeline = pipeline_factory(backend=backend)
    pid = _import(pipeline, tmp_path)
    base = paper_dir(pipeline.data_dir, pid)
    checkpoint = base / "translation" / "_checkpoints" / "experiments.json"
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    saved[field] = [] if field == "block_ids" else "stale"
    saved["translations"] = {bid: "stale text" for bid in saved["translations"]}
    checkpoint.write_text(json.dumps(saved), encoding="utf-8")
    before = sum(c["task"] == "translate" for c in backend.calls)
    _import(pipeline, tmp_path)
    assert sum(c["task"] == "translate" for c in backend.calls) > before + 1
    final = json.loads((base / "translation" / "experiments.json").read_text(encoding="utf-8"))
    assert "stale text" not in final["translations"].values()


def test_permanent_extract_failure_still_saves_other_futures(pipeline_factory, tmp_path, db):
    class Fails(FakeBackend):
        def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
            if task == "extract" and "类别 method" in user:
                raise _truncated(task)
            return super().structured(schema=schema, system=system, user=user,
                                      max_tokens=max_tokens, effort=effort, task=task)

    pipeline = pipeline_factory(backend=Fails())
    with pytest.raises(PipelineError, match="section method"):
        _import(pipeline, tmp_path)
    pid = db.list_papers()[0]["id"]
    base = paper_dir(pipeline.data_dir, pid)
    assert (base / "modules" / "experiments.json").exists()
    assert (base / "modules" / "conclusion.json").exists()
    runs = [r for r in db.runs_for(pid) if r["task"] == "extract"]
    assert sum(r["truncated"] for r in runs) == 2
    assert any(r["module"] == "conclusion" and r["status"] == "ok" for r in runs)


def test_empty_translated_blocks_remain_partial(pipeline_factory, tmp_path, db):
    class Omits(FakeBackend):
        def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
            if task in ("translate", "repair") and "We evaluate on GAIA" in user:
                return ModelReply(text='{"translations": []}', data={"translations": []})
            return super().structured(schema=schema, system=system, user=user,
                                      max_tokens=max_tokens, effort=effort, task=task)

    pipeline = pipeline_factory(backend=Omits())
    pid = _import(pipeline, tmp_path)
    assert db.get_paper(pid)["status"] == "partial"
    base = paper_dir(pipeline.data_dir, pid)
    assert not (base / "translation" / "experiments.json").exists()
    assert "missing translated blocks" in (base / "translate_warnings.json").read_text(encoding="utf-8")


def test_silent_omission_retries_only_the_missing_block(pipeline_factory, tmp_path, db):
    class OmitsInGroup(FakeBackend):
        def __init__(self):
            super().__init__()
            self.omitted = None
            self.solo_calls = 0

        def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
            reply = super().structured(schema=schema, system=system, user=user,
                                       max_tokens=max_tokens, effort=effort, task=task)
            if task == "translate" and "We evaluate on GAIA" in user:
                items = reply.data["translations"]
                if len(items) > 1:
                    self.omitted = re.search(r"^\[([^\]]+)\] We evaluate on GAIA", user, re.M)[1]
                    reply.data["translations"] = [item for item in items if item["block_id"] != self.omitted]
                elif items[0]["block_id"] == self.omitted:
                    self.solo_calls += 1
            if task == "repair" and self.omitted:
                reply.data["translations"] = [item for item in reply.data["translations"]
                                               if item["block_id"] != self.omitted]
            return reply

    backend = OmitsInGroup()
    pipeline = pipeline_factory(backend=backend)
    pid = _import(pipeline, tmp_path)
    assert backend.solo_calls == 1
    assert db.get_paper(pid)["status"] == "ready"
    assert not (paper_dir(pipeline.data_dir, pid) / "translate_warnings.json").exists()
    base = paper_dir(pipeline.data_dir, pid)
    progress = json.loads((base / "progress_meta.json").read_text(encoding="utf-8"))
    completed = [p for p in (base / "translation").glob("*.json") if not p.stem.startswith("_")]
    assert progress["translations_total"] == len(completed)


def test_long_chapter_splits_without_changing_source_blocks():
    blocks = [{"id": f"p1-b{i}", "text": "x" * 3000, "type": "text"} for i in range(6)]
    chunks = _split_for_translation(blocks)
    assert len(chunks) == 3
    assert [b for chunk in chunks for b in chunk] == blocks
    assert all(sum(len(b["text"]) for b in chunk) <= 6000 for chunk in chunks)


def test_stop_drains_completed_parallel_extracts(pipeline_factory, tmp_path, db):
    from carrel.runtime.context import RunInterrupted
    class Interrupted(FakeBackend):
        def structured(self, **kwargs):
            if kwargs.get('task') == 'extract' and '类别 method' in kwargs['user']:
                raise RunInterrupted()
            return super().structured(**kwargs)
    pipeline = pipeline_factory(backend=Interrupted())
    with pytest.raises(RunInterrupted):
        _import(pipeline, tmp_path)
    base = paper_dir(pipeline.data_dir, db.list_papers()[0]['id'])
    assert (base / 'modules' / 'experiments.json').is_file()
    assert (base / 'modules' / 'conclusion.json').is_file()
    assert not (base / 'modules' / 'method.json').exists()


def test_stop_drains_completed_parallel_translations(pipeline_factory, tmp_path, db):
    from carrel.runtime.context import RunInterrupted
    class Interrupted(FakeBackend):
        def structured(self, **kwargs):
            if kwargs.get('task') == 'translate' and 'The Director builds' in kwargs['user']:
                raise RunInterrupted()
            return super().structured(**kwargs)
    pipeline = pipeline_factory(backend=Interrupted())
    with pytest.raises(RunInterrupted):
        _import(pipeline, tmp_path)
    base = paper_dir(pipeline.data_dir, db.list_papers()[0]['id'])
    assert (base / 'translation' / 'experiments.json').is_file()
    assert (base / 'translation' / 'conclusion.json').is_file()
    assert not (base / 'translation' / 'method.json').exists()


def test_job_reports_incomplete_translation(config, db, monkeypatch):
    from types import SimpleNamespace

    from carrel.web.app import _JobRunner

    pid = "arxiv:2611.00001"
    db.upsert_paper({"id": pid, "status": "partial"})
    runner = _JobRunner(config, db)

    class Pipeline:
        def __init__(self, *args, **kwargs):
            pass

        def process(self, *args, **kwargs):
            return pid

    runner.scheduler.pipeline = Pipeline()
    runner.scheduler.finalizer = lambda batch: None
    job = runner.submit("https://arxiv.org/abs/2611.00001")
    runner.scheduler.wait(job["batch_id"], timeout=5)
    runner.scheduler.shutdown()
    record = next(j for j in db.list_jobs() if j["id"] == job["job_id"])
    assert record["status"] == "partial"
    assert record["error_code"] == "processing_incomplete"
    assert runner.progress(pid)["stage"] == "partial"
    assert runner.scheduler.status(job["batch_id"])["finalized"]

    from fastapi.testclient import TestClient
    from carrel.web.app import create_app

    client = TestClient(create_app(config, db))
    progress = client.get(f"/api/papers/{pid}/progress").json()
    assert progress["stages"]["translating"] == "failed"
