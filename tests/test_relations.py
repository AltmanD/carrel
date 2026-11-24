"""Relation metadata, public links and database compatibility."""

import sqlite3

import pytest

from carrel.models import Relation
from carrel.relate.link import RelationBundle, RelationCollector
from carrel.relate.openalex import OpenAlexClient, OpenAlexError
from carrel.relate.sciverse import SciverseClient, SciverseError
from carrel.store.db import SCHEMA_VERSION, Database


def test_reference_batch_includes_extended_records_and_retries_missing_ids(monkeypatch):
    client = OpenAlexClient()
    calls = []

    def get(path, params):
        calls.append((path, params))
        if path == "/works":
            assert params == {"filter": "openalex:W1|W2|W3", "per-page": 3, "corpus": "all"}
            return {"results": [{"id": "https://openalex.org/W2", "title": "Extended record"},
                                {"id": "https://openalex.org/W3", "title": ""}]}
        assert path in ("/works/W1", "/works/W3")
        return {"id": "https://openalex.org/" + path.rsplit("/", 1)[1], "title": "Single lookup"}

    monkeypatch.setattr(client, "_get", get)
    works = client.hydrate(["openalex:w1", "openalex:W2", "openalex:W1", "doi:10.1/x",
                            "openalex:W3", "openalex:../../bad"])
    assert [w["id"] for w in works] == [f"https://openalex.org/W{i}" for i in (1, 2, 3)]
    assert [path for path, _ in calls] == ["/works", "/works/W1", "/works/W3"]


def test_reference_fallback_failure_keeps_successful_results(monkeypatch):
    client = OpenAlexClient()

    def get(path, params):
        if path == "/works":
            return {"results": [{"id": "https://openalex.org/W1", "title": "Available"}]}
        if path == "/works/W2":
            raise OpenAlexError("HTTP 404")
        return {"id": "https://openalex.org/W99", "title": "Wrong ID"}

    monkeypatch.setattr(client, "_get", get)
    works = client.hydrate([f"openalex:W{i}" for i in (1, 2, 3)])
    assert [w["title"] for w in works] == ["Available"]


def test_reference_batch_failure_does_not_fan_out_requests(monkeypatch):
    client = OpenAlexClient()
    calls = []

    def get(path, params):
        calls.append(path)
        raise OpenAlexError("rate_limited")

    monkeypatch.setattr(client, "_get", get)
    assert client.hydrate(["openalex:W1", "openalex:W2"]) == []
    assert calls == ["/works"]


def test_reference_partial_refresh_keeps_existing_metadata():
    class Metadata:
        def hydrate(self, ids):
            return [{"id": "https://openalex.org/W1", "title": "Resolved title"}]

    row = Relation("paper", "openalex:W1", "cites", dst_doi="10.1/ref", dst_arxiv="2501.12345",
                   dst_url="https://example.org/ref", dst_year=2025)
    RelationCollector(openalex=Metadata()).hydrate_cites(RelationBundle(cites=[row]))
    assert row.dst_title == "Resolved title"
    assert (row.dst_doi, row.dst_arxiv, row.dst_url, row.dst_year) == (
        "10.1/ref", "2501.12345", "https://example.org/ref", 2025)


def test_similarity_metadata_keeps_ranking_and_resolves_exact_work(monkeypatch):
    client = SciverseClient(token="test")

    def post(path, payload):
        if path == "/agentic-search":
            return {"hits": [{"doc_id": "abc", "title": "A paper", "score": .91}]}
        assert path == "/meta-search"
        return {"results": [
            {"doc_id": "other", "title": "Another paper", "doi": "10.1/wrong"},
            {"doc_id": "abc", "title": "A Paper", "doi": "https://doi.org/10.48550/arXiv.2501.12345",
             "publication_published_year": 2025.0,
             "locations": [{"url": "https://arxiv.org/pdf/2501.12345v2"}]},
        ]}

    monkeypatch.setattr(client, "_post", post)
    row = client.similar(query="A paper")[0]
    assert row.dst_title == "A Paper"
    assert row.dst_doi == "10.48550/arXiv.2501.12345"
    assert row.dst_arxiv == "2501.12345"
    assert row.dst_year == 2025
    assert row.dst_url == "https://arxiv.org/pdf/2501.12345v2"
    assert row.rank == 0 and row.score == .91 and row.provider == "sciverse"


def test_similarity_does_not_link_to_a_different_title(monkeypatch):
    client = SciverseClient(token="test")
    row = Relation("paper", "sciverse:abc", "similar", dst_title="Intended paper")
    monkeypatch.setattr(client, "_post", lambda *args: {"results": [
        {"doc_id": "other", "title": "Unrelated paper", "doi": "10.1/wrong"}]})
    assert client.hydrate([row]) == 0
    assert not row.dst_doi and not row.dst_url
    assert row.dst_title == "Intended paper"


def test_similarity_keeps_results_when_metadata_fails(monkeypatch):
    client = SciverseClient(token="test")

    def post(path, payload):
        if path == "/agentic-search":
            return {"hits": [{"doc_id": "abc", "title": "A paper", "score": .9}]}
        raise SciverseError("metadata unavailable")

    monkeypatch.setattr(client, "_post", post)
    rows = client.similar(query="A paper")
    assert len(rows) == 1 and rows[0].dst_title == "A paper"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,test", "http://[invalid"])
def test_similarity_rejects_unsafe_or_malformed_urls(monkeypatch, url):
    client = SciverseClient(token="test")
    row = Relation("paper", "sciverse:abc", "similar", dst_title="A paper")
    monkeypatch.setattr(client, "_post", lambda *args: {"results": [
        {"doc_id": "abc", "title": "A paper", "locations": [{"url": url}]}]})
    assert client.hydrate([row]) == 0
    assert row.dst_url == ""


def test_metadata_without_doi_uses_publisher_url(monkeypatch):
    client = SciverseClient(token="test")
    row = Relation("paper", "sciverse:abc", "similar", dst_title="A paper")
    monkeypatch.setattr(client, "_post", lambda *args: {"results": [
        {"doc_id": "abc", "title": "A paper", "access_oa_url": "https://example.org/paper"}]})
    assert client.hydrate([row]) == 1
    assert row.dst_url == "https://example.org/paper"


def test_reference_hydration_handles_display_name_and_incomplete_metadata():
    class Metadata:
        def hydrate(self, ids):
            return [{"id": "https://openalex.org/W1", "display_name": "Reference title",
                     "doi": "https://doi.org/10.1/ref"}]

    rows = [Relation("paper", f"openalex:W{i}", "cites", coverage="partial") for i in (1, 2)]
    bundle = RelationBundle(cites=rows)
    RelationCollector(openalex=Metadata()).hydrate_cites(bundle)
    assert rows[0].dst_title == "Reference title" and rows[0].dst_doi == "10.1/ref"
    assert rows[0].coverage == "complete" and rows[1].coverage == "partial"
    assert bundle.coverage["cites"] == "partial"


def test_database_migrates_existing_relation_without_losing_it(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('version', '1');
            CREATE TABLE relations (
                src_id TEXT, dst_id TEXT, kind TEXT, dst_title TEXT DEFAULT '',
                dst_year INTEGER, dst_arxiv TEXT DEFAULT '', dst_doi TEXT DEFAULT '',
                provider TEXT DEFAULT '', coverage TEXT DEFAULT 'complete', rank INTEGER DEFAULT 0,
                score REAL, fetched_at TEXT DEFAULT '', PRIMARY KEY(src_id,dst_id,kind)
            );
            INSERT INTO relations(src_id,dst_id,kind,dst_title) VALUES('paper','other','similar','Saved paper');
        """)
    db = Database(path)
    try:
        assert db.version == SCHEMA_VERSION
        row = db.relations_for("paper")[0]
        assert row["dst_title"] == "Saved paper" and row["dst_url"] == ""
        row["dst_url"] = "https://example.org/paper"
        db.replace_relations("paper", "similar", [row])
    finally:
        db.close()
    db = Database(path)
    try:
        assert db.relations_for("paper")[0]["dst_url"] == "https://example.org/paper"
    finally:
        db.close()
