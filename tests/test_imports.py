"""Mixed-text imports, metadata fallbacks and confirmation without side effects."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from carrel.ids import UnsupportedInput, extract_inputs, resolve_input
from carrel.web import imports
from carrel.web.app import _JobRunner, create_app


def test_extracts_prose_markdown_and_same_line_links_in_order():
    text = """Reading list, unrelated commentary and https://example.com/news
    [First](https://huggingface.co/papers/2501.12948), plus https://arxiv.org/pdf/2501.12948v3.pdf?download=1
    Second: https://arxiv.org/html/2401.12345v2; (arxiv.org/abs/hep-th/9901001).
    https://huggingface.co/papers?date=2026-11-17
    """
    items, duplicates = extract_inputs(text)
    assert [item.paper_id for item in items] == ["arxiv:2501.12948", "arxiv:2401.12345", "arxiv:hep-th/9901001"]
    assert items[0].source_url == "https://huggingface.co/papers/2501.12948"
    assert duplicates == 1


@pytest.mark.parametrize("text", [
    "https://notarxiv.org/abs/2501.12948", "https://arxiv.org.evil.test/abs/2501.12948",
    "https://example.com/?next=https://arxiv.org/abs/2501.12948",
    "https://user@arxiv.org/abs/2501.12948", "https://arxiv.org/abs/2501.123456",
    "https://huggingface.co/papers/2501.12948broken", "unrelated commentary",
])
def test_does_not_extract_lookalikes_or_partial_identifiers(text):
    assert extract_inputs(text) == ([], 0)


@pytest.mark.parametrize("text", [
    "https://arxiv.org/abs/2501.123456", "https://arxiv.org/abs/2501.12948/extra",
    "https://user@arxiv.org/abs/2501.12948", "https://notarxiv.org/abs/2501.12948",
    "https://arxiv.org/abs/2501.12948 and https://arxiv.org/abs/2401.12345",
])
def test_single_import_accepts_only_one_supported_locator(text):
    with pytest.raises(UnsupportedInput):
        resolve_input(text)


def test_preserves_pdf_urls_paths_and_arxiv_scheme(tmp_path):
    pdf = tmp_path / "a paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    items, _ = extract_inputs(f'notes\n"{pdf}"\nhttps://example.com/paper.pdf?token=123\narxiv:2501.12948v2')
    assert [item.kind for item in items] == ["local_path", "pdf_url", "arxiv"]
    assert items[0].locator == str(pdf.resolve())


@pytest.mark.parametrize("source", ["https://arxiv.org/abs/2501.12948", "https://huggingface.co/papers/2501.12948"])
def test_fetches_source_title_and_decodes_entities(source):
    def respond(request):
        if request.url.host == "huggingface.co":
            return httpx.Response(200, json={"id": "2501.12948", "title": "A & B"})
        return httpx.Response(200, text='<meta name="citation_title" content="A &amp; B">')

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await imports.fetch_title(resolve_input(source), client)

    assert asyncio.run(fetch()) == "A & B"


def test_unavailable_arxiv_falls_back_to_hf():
    requests = []

    def respond(request):
        requests.append(request.url.host)
        if request.url.host == "arxiv.org":
            return httpx.Response(503)
        return httpx.Response(200, json={"id": "2501.12948", "title": "Verified title"})

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await imports.fetch_title(resolve_input("arxiv:2501.12948"), client)

    assert asyncio.run(fetch()) == "Verified title"
    assert requests == ["arxiv.org", "huggingface.co"]


def test_error_page_and_wrong_paper_cannot_supply_title():
    def respond(request):
        if request.url.host == "arxiv.org":
            return httpx.Response(200, text="<title>Access denied</title>")
        return httpx.Response(200, json={"id": "9999.99999", "title": "Unrelated paper"})

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await imports.fetch_title(resolve_input("arxiv:2501.12948"), client)

    assert asyncio.run(fetch()) == ""


def test_preview_deduplicates_and_never_imports(config, db, monkeypatch):
    fetched = []

    async def title(item, client):
        fetched.append(item.arxiv_id)
        return "Title from source"

    monkeypatch.setattr(imports, "fetch_title", title)
    app = create_app(config, db)
    submit = app.state.jobs.submit = Mock()
    with TestClient(app) as client:
        response = client.post("/api/import/preview", json={"input": "some notes https://huggingface.co/papers/2501.12948\nhttps://arxiv.org/pdf/2501.12948v2"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["papers"]) == 1 and data["duplicates"] == 1
    assert data["papers"][0]["title"] == "Title from source"
    assert fetched == ["2501.12948"]
    submit.assert_not_called()
    assert db.list_papers() == [] and db.list_jobs() == []


def test_preview_reuses_library_titles_and_handles_missing_metadata(config, db, monkeypatch):
    db.upsert_paper({"id": "arxiv:2501.12948", "title": "Local title", "status": "ready"})

    async def missing(item, client):
        assert item.arxiv_id == "2401.12345"
        return ""

    monkeypatch.setattr(imports, "fetch_title", missing)
    result = asyncio.run(imports.preview_imports("arxiv:2501.12948\narxiv:2401.12345", db))
    first, second = result["papers"]
    assert first["title"] == "Local title" and first["existing"] is True
    assert second["title"] == "" and second["title_status"] == "unavailable"
    assert second["input"] == "https://arxiv.org/abs/2401.12345"


@pytest.mark.parametrize("value", [None, [], "", "notes only", "x" * 100_001,
    "\n".join(f"arxiv:2501.{index:05d}" for index in range(51))])
def test_preview_rejects_empty_unsupported_and_oversized_batches(config, db, value):
    with TestClient(create_app(config, db)) as client:
        response = client.post("/api/import/preview", json={"input": value})
    assert response.status_code == 400
    assert db.list_jobs() == []


def test_confirmed_title_reaches_job_queue(config, db):
    app = create_app(config, db)
    submit = app.state.jobs.submit = Mock(return_value={"paper_id": "arxiv:2501.12948", "job_id": "test"})
    with TestClient(app) as client:
        assert client.post("/api/import", json={"input": "arxiv:2501.12948", "title": " Edited title ", "force": True}).status_code == 200
        assert client.post("/api/import", json={"input": "arxiv:2501.12948", "title": []}).status_code == 400
    from carrel.profile import PRESETS
    submit.assert_called_once_with("arxiv:2501.12948", title="Edited title", force=True, profile=PRESETS['skim'].to_dict())


def test_worker_passes_confirmed_title_to_pipeline(config, db, monkeypatch):
    from carrel import pipeline as pipeline_module

    pipeline = Mock()
    pipeline.process.return_value = "arxiv:2501.12948"
    monkeypatch.setattr("carrel.scheduler.Pipeline", Mock(return_value=pipeline))
    runner = _JobRunner(config, db)
    runner.scheduler.finalizer = lambda batch: None
    job = runner.submit("arxiv:2501.12948", title="Confirmed title")
    runner.scheduler.wait(job["batch_id"], timeout=5)
    runner.scheduler.shutdown()
    assert pipeline.process.call_args.kwargs["title"] == "Confirmed title"


@pytest.mark.parametrize("source", ["arxiv:2501.12948", "local"])
def test_confirmed_title_survives_processing_and_reimport(pipeline_factory, tmp_path, db, source):
    if source == "local":
        pdf = tmp_path / "confirmed.pdf"
        pdf.write_bytes(b"%PDF-1.4 confirmed")
        source = str(pdf)
    pipeline = pipeline_factory()
    paper_id = pipeline.process(source, title="Confirmed title")
    assert db.get_paper(paper_id)["title"] == "Confirmed title"
    calls = len(db.runs_for(paper_id))
    assert pipeline.process(source, title="Corrected title") == paper_id
    assert db.get_paper(paper_id)["title"] == "Corrected title"
    assert len(db.runs_for(paper_id)) == calls
