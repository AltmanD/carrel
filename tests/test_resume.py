"""Stage-level resume: a retry re-runs only what is missing.

Before this, every retry — including the web "重试" button on a `partial`
paper — re-parsed the PDF and re-ran every model call. On the two real papers
in test-kb, that was two full extra passes to recover a single failed module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carrel.store.paths import paper_dir
from tests.conftest import FakeBackend, FakeParser


def _import(pipeline, tmp_path: Path, name: str = "paper.pdf", **kw) -> str:
    pdf = tmp_path / name
    if not pdf.exists():
        pdf.write_bytes(b"%PDF-1.4 " + name.encode())
    return pipeline.process(str(pdf), **kw)


def _calls(backend: FakeBackend, task: str) -> int:
    return sum(1 for c in backend.calls if c["task"] == task)


class FailOnceBackend(FakeBackend):
    """Fails the first translate call for one module, then behaves."""

    def __init__(self, *, fail_module_text: str) -> None:
        super().__init__()
        self.fail_module_text = fail_module_text
        self.failed_once = False

    def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
        from carrel.model import ModelError

        if task == "translate" and not self.failed_once and self.fail_module_text in user:
            self.failed_once = True
            raise ModelError("simulated gateway failure")
        return super().structured(schema=schema, system=system, user=user,
                                  max_tokens=max_tokens, effort=effort, task=task)


class TestResume:
    def test_partial_retry_only_retranslates_the_failed_module(self, pipeline_factory, tmp_path, db):
        backend = FailOnceBackend(fail_module_text="The Director builds an agent graph")
        parser = FakeParser()
        pipeline = pipeline_factory(backend=backend, parser=parser)
        pid = _import(pipeline, tmp_path)
        assert db.get_paper(pid)["status"] == "partial"
        base = paper_dir(pipeline.data_dir, pid)
        failed = json.loads((base / "translate_warnings.json").read_text(encoding="utf-8"))
        assert failed["failed_sections"] == ["method"]

        before = {t: _calls(backend, t) for t in
                  ("preview", "extract", "summarize", "glossary", "translate", "classify", "assess")}
        parses_before = parser.calls

        pid2 = _import(pipeline, tmp_path)  # plain retry: no force
        assert pid2 == pid
        assert db.get_paper(pid)["status"] == "ready"
        after = {t: _calls(backend, t) for t in before}

        # Nothing upstream of the failed module was paid for again...
        assert parser.calls == parses_before, "MinerU must not re-parse"
        for task in ("preview", "extract", "summarize", "glossary", "classify", "assess"):
            assert after[task] == before[task], task
        # ...and exactly one module was translated.
        assert after["translate"] == before["translate"] + 1
        assert not (base / "translate_warnings.json").exists()
        tr = json.loads((base / "translation" / "method.json").read_text(encoding="utf-8"))
        assert tr["translations"], "the failed module now has a translation"

    def test_force_reruns_everything(self, pipeline_factory, tmp_path, db):
        backend = FakeBackend()
        parser = FakeParser()
        pipeline = pipeline_factory(backend=backend, parser=parser)
        pid = _import(pipeline, tmp_path)
        before = {t: _calls(backend, t) for t in ("extract", "summarize", "translate", "classify")}
        _import(pipeline, tmp_path, force=True)
        assert parser.calls == 2
        for task, n in before.items():
            assert _calls(backend, task) == 2 * n, task

    def test_ready_paper_without_force_is_a_no_op(self, pipeline_factory, tmp_path, db):
        backend = FakeBackend()
        pipeline = pipeline_factory(backend=backend)
        _import(pipeline, tmp_path)
        n = len(backend.calls)
        _import(pipeline, tmp_path)
        assert len(backend.calls) == n

    def test_changed_document_invalidates_artefacts(self, pipeline_factory, tmp_path, db):
        from tests.conftest import sample_content_list

        backend = FakeBackend()
        pipeline = pipeline_factory(backend=backend)
        pid = _import(pipeline, tmp_path)
        base = paper_dir(pipeline.data_dir, pid)
        before = {t: _calls(backend, t) for t in ("extract", "summarize", "translate", "classify")}

        # Same record, but the stored parse is gone (or a parser upgrade yields a
        # different block set): the derived artefacts carry the old fingerprint
        # and must be treated as stale rather than matched by file name.
        content = sample_content_list()
        content.insert(2, {"type": "text", "text": "A brand new paragraph with 7 facts.", "page_idx": 0})
        pipeline.parser = FakeParser(content=content)
        (base / "doc.json").unlink()
        db.set_status(pid, "partial")
        pid2 = _import(pipeline, tmp_path)
        assert pid2 == pid
        for task, n in before.items():
            assert _calls(backend, task) > n, f"{task} must be redone for a changed doc"
        # and the new artefacts carry the new fingerprint
        doc = json.loads((base / "doc.json").read_text(encoding="utf-8"))
        summary = json.loads((base / "summary.json").read_text(encoding="utf-8"))
        from carrel.models import DocModel
        assert summary["doc_fingerprint"] == DocModel.from_dict(doc).fingerprint()

    def test_truncated_summary_is_rebuilt_on_resume(self, pipeline_factory, tmp_path, db):
        backend = FakeBackend()
        pipeline = pipeline_factory(backend=backend)
        pid = _import(pipeline, tmp_path)
        base = paper_dir(pipeline.data_dir, pid)
        summary = json.loads((base / "summary.json").read_text(encoding="utf-8"))
        summary["one_line_source"] = "truncated"
        (base / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        db.set_status(pid, "partial")
        n_sum = _calls(backend, "summarize")
        n_ext = _calls(backend, "extract")
        _import(pipeline, tmp_path)
        assert _calls(backend, "summarize") == n_sum + 1, "a cut-off summary is not a finished one"
        assert _calls(backend, "extract") == n_ext, "but the extracts behind it are reused"


class TestRetryEndpoint:
    def test_retry_uses_stored_source_and_defaults_to_resume(self, config, db):
        from fastapi.testclient import TestClient

        from carrel.web import app as web_app

        submitted = []

        class Jobs:
            def submit(self, raw, *, force=False, paper_id="", profile=None):
                submitted.append((raw, force, paper_id))
                return {"job_id": "j", "paper_id": paper_id or "p"}

        application = web_app.create_app(config, db)
        application.state.jobs = Jobs()
        client = TestClient(application)
        db.upsert_paper({"id": "pdf-abc", "title": "t", "status": "partial",
                         "source_path": "/tmp/paper.pdf", "created_at": "", "updated_at": ""})
        r = client.post("/api/papers/pdf-abc/retry", headers={'X-Carrel':'1'})
        assert r.status_code == 200
        assert submitted[-1] == ("/tmp/paper.pdf", False, "pdf-abc")
        r = client.post("/api/papers/pdf-abc/retry?force=1", headers={'X-Carrel':'1'})
        assert submitted[-1] == ("/tmp/paper.pdf", True, "pdf-abc")
