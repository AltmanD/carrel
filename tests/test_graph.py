"""Offline tests for the read-only knowledge graph."""

from __future__ import annotations

import json

import pytest

from carrel.config import AppConfig
from carrel.graph.build import (
    build_library_graph,
    build_paper_graph,
    short_label,
)
from carrel.graph.identity import (
    AliasIndex,
    ExternalRegistry,
    category_key,
    normalize_arxiv,
    normalize_doi,
    normalize_title,
)
from carrel.graph.links import relation_link


def _categories(discipline="计算机科学", field="机器学习", subfield="强化学习"):
    return [{"discipline": discipline, "field": field, "subfield": subfield}]


def _paper(pid, title, **extra):
    return {"id": pid, "title": title, "status": "ready", "discipline": "计算机科学",
            "field": "机器学习", "year": extra.pop("year", None), **extra}


def _library(papers, categories=None, tags=(), relations=(), **kwargs):
    categories = categories or {}
    return build_library_graph(
        papers=papers,
        categories_for=lambda p: categories.get(p["id"], []),
        tags=list(tags),
        relations=list(relations),
        link_resolver=lambda r: relation_link(r, local_lookup=lambda pid: None),
        **kwargs,
    )


class TestIdentity:
    def test_arxiv_version_is_dropped(self):
        assert normalize_arxiv("arXiv:2402.03300v3") == "2402.03300"
        assert normalize_arxiv("https://arxiv.org/pdf/2501.12948v2") == "2501.12948"

    def test_doi_prefix_and_case_normalised(self):
        assert normalize_doi("https://doi.org/10.1038/S41586-025-09422-Z") == "10.1038/s41586-025-09422-z"

    def test_title_key_keeps_math_and_punctuation(self):
        assert normalize_title("  Dense  Rewards $x^2$ ") == "dense rewards $x^2$"
        assert normalize_title("Foo-bar") != normalize_title("Foo bar")

    def test_category_key_is_unambiguous_for_slashes(self):
        assert category_key({"discipline": "A/B", "field": "C"}) != \
            category_key({"discipline": "A", "field": "B/C"})
        assert category_key({"discipline": "计算机科学", "field": "机器学习", "subfield": ""}) == \
            category_key({"discipline": "计算机科学", "field": "机器学习"})

    def test_alias_prefers_identifiers_over_title(self):
        index = AliasIndex.from_papers([
            {"id": "arxiv:2402.03300", "title": "DeepSeekMath"},
            {"id": "doi:10.1038/s41586-025-09422-z", "title": "R1"},
        ])
        assert index.local_paper({"dst_id": "openalex:W1", "dst_arxiv": "2402.03300v2"}) == "arxiv:2402.03300"
        assert index.local_paper({"dst_id": "openalex:W2", "dst_doi": "10.48550/arXiv.2402.03300"}) == ""
        # The DOI row only resolves through a DOI spelled in a different case.
        assert index.local_paper({"dst_id": "openalex:W3",
                                  "dst_doi": "https://doi.org/10.1038/S41586-025-09422-Z"}
                                 ) == "doi:10.1038/s41586-025-09422-z"
        # A known identifier that matches nothing must not fall back to a title.
        assert index.local_paper({"dst_id": "openalex:W4", "dst_arxiv": "1111.11111",
                                  "dst_title": "DeepSeekMath"}) == ""

    def test_title_fallback_needs_a_conflict_free_match(self):
        index = AliasIndex.from_papers([
            {"id": "a", "title": "Same Title", "year": 2024},
            {"id": "b", "title": "Same Title", "year": 2020},
        ])
        # Two library papers share the title: ambiguous, so no match.
        assert index.local_paper({"dst_id": "openalex:W9", "dst_title": "Same Title"}) == ""

    def test_title_fallback_rejects_a_conflicting_year(self):
        index = AliasIndex.from_papers([{"id": "a", "title": "Only One", "year": 2024}])
        assert index.local_paper({"dst_id": "x", "dst_title": "only  one", "dst_year": 2024}) == "a"
        assert index.local_paper({"dst_id": "x", "dst_title": "only one", "dst_year": 2019}) == ""

    def test_records_without_ids_never_merge(self):
        registry = ExternalRegistry()
        registry.add({"dst_id": "", "dst_title": "", "rank": 0, "src_id": "p1", "kind": "cites"})
        registry.add({"dst_id": "", "dst_title": "", "rank": 1, "src_id": "p1", "kind": "cites"})
        assert len(registry.resolve()) == 2

    def test_same_work_with_two_identifiers_merges(self):
        registry = ExternalRegistry()
        registry.add({"dst_id": "openalex:W100", "dst_arxiv": "2402.03300", "dst_title": "A"})
        registry.add({"dst_id": "openalex:W200", "dst_doi": "10.1/x", "dst_title": "A"})
        registry.add({"dst_id": "openalex:W300", "dst_arxiv": "2402.03300",
                      "dst_doi": "10.1/x", "dst_title": "A (full)"})
        keys = registry.resolve()
        assert len(keys) == 1
        (entry,) = keys.values()
        assert entry.arxiv == "2402.03300" and entry.doi == "10.1/x"


class TestLibraryGraph:
    def test_empty_library_returns_empty_topology(self):
        graph = _library([])
        assert graph.nodes == [] and graph.edges == []
        assert graph.stats["papers"] == 0

    def test_shared_subfield_links_two_papers_without_quoting(self):
        papers = [_paper("p1", "Alpha"), _paper("p2", "Beta")]
        graph = _library(papers, categories={"p1": _categories(), "p2": _categories()})
        role_edges = [e for e in graph.edges if e.role == "similar"]
        assert not role_edges  # shared category is membership, not a citation
        paper_edges = [e for e in graph.edges if e.role == "membership" and e.source.startswith("paper:")]
        assert len(paper_edges) == 2
        targets = {e.target for e in paper_edges}
        assert len(targets) == 1  # both join the same leaf category

    def test_only_one_membership_edge_is_primary(self):
        papers = [_paper("p1", "Alpha")]
        graph = _library(papers, categories={"p1": [
            {"discipline": "计算机科学", "field": "机器学习", "subfield": "强化学习"},
            {"discipline": "计算机科学", "field": "自然语言处理", "subfield": "智能体"},
        ]})
        memberships = [e for e in graph.edges if e.role == "membership" and e.source.startswith("paper:")]
        assert sum(e.primary for e in memberships) == 1
        assert len(memberships) == 2  # the other path stays as a cross edge

    def test_shared_tag_becomes_a_node_and_single_tag_does_not(self):
        papers = [_paper("p1", "Alpha"), _paper("p2", "Beta")]
        tags = [{"paper_id": "p1", "tag": "shared"}, {"paper_id": "p2", "tag": "shared"},
                {"paper_id": "p1", "tag": "private"}]
        graph = _library(papers, tags=tags)
        tag_nodes = [n for n in graph.nodes if n.role == "tag"]
        assert [n.label for n in tag_nodes] == ["shared"]

    def test_unreadable_papers_are_excluded(self):
        papers = [_paper("p1", "Alpha"), {**_paper("p2", "Beta"), "status": "queued"}]
        graph = _library(papers)
        assert graph.stats["papers"] == 1
        assert {n.meta.get("paper_id") for n in graph.nodes if n.role == "paper"} == {"p1"}

    def test_cited_by_is_flipped_into_citing_to_cited(self):
        papers = [_paper("p1", "Alpha"), _paper("p2", "Beta")]
        relations = [{"src_id": "p1", "dst_id": "p2", "kind": "cited_by", "provider": "openalex"}]
        graph = _library(papers, relations=relations)
        edge = next(e for e in graph.edges if e.role == "cites")
        assert edge.source == "paper:p2" and edge.target == "paper:p1"

    def test_similar_edges_are_undirected_and_deduped(self):
        papers = [_paper("p1", "Alpha"), _paper("p2", "Beta")]
        relations = [
            {"src_id": "p1", "dst_id": "p2", "kind": "similar"},
            {"src_id": "p2", "dst_id": "p1", "kind": "similar"},
        ]
        graph = _library(papers, relations=relations)
        assert len([e for e in graph.edges if e.role == "similar"]) == 1

    def test_reference_mirrors_merge_evidence_but_reciprocal_edges_survive(self):
        papers = [_paper("p1", "Alpha"), _paper("p2", "Beta")]
        relations = [
            {"src_id": "p1", "dst_id": "p2", "kind": "cites", "provider": "one"},
            {"src_id": "p2", "dst_id": "p1", "kind": "cited_by", "provider": "two"},
            {"src_id": "p2", "dst_id": "p1", "kind": "cites", "provider": "one"},
            {"src_id": "p1", "dst_id": "p1", "kind": "cites"},
        ]
        graph = _library(papers, relations=relations)
        citations = [e for e in graph.edges if e.role == "cites"]
        assert {(e.source, e.target) for e in citations} == {
            ("paper:p1", "paper:p2"), ("paper:p2", "paper:p1")}
        assert len(citations) == 2
        assert next(e for e in citations if e.source == "paper:p1").evidence == "one · two"

    def test_ordering_is_independent_of_input_order(self):
        papers = [_paper("p1", "Alpha"), _paper("p2", "Beta")]
        relations = [{"src_id": "p1", "dst_id": "p2", "kind": "cites"}]
        first = _library(papers, relations=relations).to_dict()
        second = _library(list(reversed(papers)), relations=relations).to_dict()
        assert [n["id"] for n in first["nodes"]] == [n["id"] for n in second["nodes"]]

    def test_every_edge_endpoint_has_a_node(self):
        papers = [_paper("p1", "Alpha"), _paper("p2", "Beta")]
        relations = [
            {"src_id": "p1", "dst_id": "p2", "kind": "cites"},
            {"src_id": "p1", "dst_id": "openalex:W9", "kind": "cites",
             "dst_title": "Outside", "dst_arxiv": "2402.03300"},
        ]
        graph = _library(papers, categories={"p1": _categories()}, relations=relations,
                         include_external=True, max_external=5)
        ids = {n.id for n in graph.nodes}
        for edge in graph.edges:
            assert edge.source in ids and edge.target in ids

    def test_external_limit_reports_honest_counts(self):
        papers = [_paper("p1", "Alpha")]
        relations = [
            {"src_id": "p1", "dst_id": f"openalex:W{i}", "kind": "cites", "dst_title": f"Work {i}"}
            for i in range(6)
        ]
        graph = _library(papers, relations=relations, include_external=True, max_external=3)
        external = [n for n in graph.nodes if n.role == "external"]
        assert len(external) == 3
        assert graph.stats["external"]["available"] == 6
        assert graph.stats["external"]["omitted"] == 3

    def test_external_hidden_by_default(self):
        papers = [_paper("p1", "Alpha")]
        relations = [{"src_id": "p1", "dst_id": "openalex:W1", "kind": "cites", "dst_title": "X"}]
        graph = _library(papers, relations=relations)
        assert not [n for n in graph.nodes if n.role == "external"]

    def test_missing_classification_falls_back_to_two_level_fields(self):
        graph = _library([_paper("p1", "Alpha")])
        notes = " ".join(graph.notes)
        assert "兜底" in notes
        assert [n for n in graph.nodes if n.role == "category"]


class TestPaperGraph:
    def _graph(self, papers, *, categories=None, tags=(), relations=(), **kwargs):
        categories = categories or {}
        focus = papers[0]
        return build_paper_graph(
            paper=focus, categories=categories.get(focus["id"], []),
            tags=[t for t in tags if t["paper_id"] == focus["id"]],
            relations=relations, papers_by_id={p["id"]: p for p in papers},
            categories_by_id=categories, library_tags=tags,
            link_resolver=lambda r: relation_link(r, local_lookup=lambda pid: None),
            **kwargs)

    def test_shared_tags_and_specific_categories_create_real_paths(self):
        papers = [_paper("p1", "Focus"), _paper("tagged", "Tagged", field="Vision"),
                  _paper("category", "Category"), _paper("broad", "Broad", field="Databases"),
                  _paper("queued", "Not ready", status="queued")]
        tags = [{"paper_id": pid, "tag": "GRPO"} for pid in ("p1", "tagged")]
        graph = self._graph(papers, tags=tags)
        ids = {n.id for n in graph.nodes}
        assert {"paper:p1", "paper:tagged", "paper:category"} <= ids
        assert "paper:broad" not in ids and "paper:queued" not in ids
        assert not [e for e in graph.edges if e.role in ("similar", "cites")]
        peer = next(n for n in graph.nodes if n.id == "paper:tagged")
        assert "共享标签：GRPO" in peer.meta["reasons"]
        focus_targets = {e.target for e in graph.edges if e.source == graph.focus}
        assert any(e.source == peer.id and e.target in focus_targets for e in graph.edges)
        renamed = [{**t, "tag": "Renamed" if t["paper_id"] == "p1" else t["tag"]} for t in tags]
        updated = self._graph(papers, tags=renamed)
        assert "paper:tagged" not in {n.id for n in updated.nodes}
        assert updated.revision != graph.revision

    def test_direct_local_relations_rank_first_and_quota_is_shared(self):
        papers = [_paper("focus", "Focus")] + [_paper(f"p{i}", f"Paper {i}") for i in range(8)]
        relations = [{"src_id": "focus", "dst_id": "p7", "kind": "cites"}]
        relations += [{"src_id": "focus", "dst_id": f"openalex:W{i}", "kind": "cites",
                       "dst_title": f"External {i}"} for i in range(8)]
        graph = self._graph(papers, relations=relations)
        assert graph.stats["selection"]["shown"] == 5
        assert "paper:p7" in {n.id for n in graph.nodes}
        assert graph.stats["external"]["shown"] == 0
        expanded = self._graph(papers, relations=relations, max_papers=12)
        assert expanded.stats["selection"]["shown"] == 12
        assert expanded.stats["external"]["shown"] == 3
        reversed_graph = self._graph([papers[0], *reversed(papers[1:])], relations=list(reversed(relations)))
        assert graph.to_dict() == reversed_graph.to_dict()

    def test_incoming_relations_and_self_links(self):
        papers = [_paper("focus", "Focus"), _paper("other", "Other", field="Vision")]
        relations = [{"src_id": "other", "dst_id": "focus", "kind": "cites"},
                     {"src_id": "focus", "dst_id": "other", "kind": "cited_by"},
                     {"src_id": "focus", "dst_id": "focus", "kind": "similar"}]
        graph = self._graph(papers, relations=relations)
        citations = [e for e in graph.edges if e.role == "cites"]
        assert len(citations) == 1
        assert citations[0].source == "paper:other" and citations[0].target == "paper:focus"
        assert not [e for e in graph.edges if e.source == e.target]
        assert graph.stats["external"]["shown"] == 0

    def test_external_title_relevance_and_identity_deduplication(self):
        paper = _paper("focus", "GRPO dense rewards")
        relations = [{"src_id": "focus", "dst_id": f"openalex:W{i}", "kind": "cites",
                      "dst_title": f"Unrelated work {i}", "rank": i} for i in range(8)]
        relations += [
            {"src_id": "focus", "dst_id": "openalex:W99", "dst_arxiv": "2402.03300",
             "kind": "cites", "dst_title": "GRPO with dense rewards", "rank": 99},
            {"src_id": "focus", "dst_id": "sciverse:dup", "dst_arxiv": "2402.03300",
             "kind": "similar", "dst_title": "GRPO with dense rewards"},
            {"src_id": "focus", "dst_id": "openalex:W100", "kind": "cites",
             "dst_title": "openalex:W100"},
        ]
        tags = [{"paper_id": "focus", "tag": "GRPO"}]
        graph = self._graph([paper], relations=relations, tags=tags)
        nodes = [n for n in graph.nodes if n.title == "GRPO with dense rewards"]
        assert len(nodes) == 1
        assert "标题含标签：GRPO" in nodes[0].meta["reasons"]
        assert graph.stats["external"]["available"] == 9
        assert graph.stats["selection"]["filtered"] == 1
        assert any(e.source == nodes[0].id and e.role == "tag" for e in graph.edges)
        no_external = self._graph([paper], relations=relations, tags=tags, include_external=False)
        assert no_external.stats["selection"]["shown"] == 1
        assert not [n for n in no_external.nodes if n.role == "external"]

    def test_focus_paper_is_flagged_and_first(self):
        paper = _paper("p1", "Alpha")
        graph = build_paper_graph(
            paper=paper, categories=_categories(), tags=[], relations=[],
            papers_by_id={"p1": paper},
            link_resolver=lambda r: relation_link(r, local_lookup=lambda pid: None))
        assert graph.focus == "paper:p1"
        assert graph.nodes[0].focus is True

    def test_no_relations_still_shows_categories_and_tags(self):
        paper = _paper("p1", "Alpha")
        graph = build_paper_graph(
            paper=paper, categories=_categories(), tags=[{"tag": "only"}],
            relations=[], papers_by_id={"p1": paper},
            link_resolver=lambda r: relation_link(r, local_lookup=lambda pid: None))
        roles = {n.role for n in graph.nodes}
        assert {"paper", "category", "tag"} <= roles

    def test_default_limit_counts_focus_local_and_external_papers(self):
        paper = _paper("p1", "Alpha")
        relations = [{"src_id": "p1", "dst_id": f"openalex:W{i}", "kind": "cites",
                      "dst_title": f"Cited {i}", "rank": i} for i in range(20)]
        relations += [{"src_id": "p1", "dst_id": f"sciverse:s{i}", "kind": "similar",
                       "dst_title": f"Similar {i}", "rank": i} for i in range(20)]
        graph = build_paper_graph(
            paper=paper, categories=[], tags=[], relations=relations,
            papers_by_id={"p1": paper},
            link_resolver=lambda r: relation_link(r, local_lookup=lambda pid: None),
        )
        assert graph.stats["external"]["shown"] == 4
        assert graph.stats["external"]["available"] == 40
        assert graph.stats["selection"]["shown"] == 5
        assert all(n.link_kind == "source" for n in graph.nodes if n.role == "external")

    def test_missing_external_title_is_filtered_out(self):
        paper = _paper("p1", "Alpha")
        graph = build_paper_graph(
            paper=paper, categories=[], tags=[],
            relations=[{"src_id": "p1", "dst_id": "sciverse:x", "kind": "similar"}],
            papers_by_id={"p1": paper},
            link_resolver=lambda r: relation_link(r, local_lookup=lambda pid: None))
        assert not [n for n in graph.nodes if n.role == "external"]
        assert graph.stats["selection"]["filtered"] == 1


class TestLinks:
    def test_local_paper_links_to_reading_page(self):
        row = relation_link({"dst_id": "arxiv:1", "dst_title": "X"},
                            local_lookup=lambda pid: {"id": pid, "title": "Local title"})
        assert row["link_kind"] == "local" and row["url"] == "/papers/arxiv:1"
        assert row["label"] == "Local title"

    def test_bare_identifier_is_never_a_title(self):
        row = relation_link({"dst_id": "openalex:W6929328961", "dst_title": "openalex:W6929328961"},
                            local_lookup=lambda pid: None)
        assert row["label"] == "文献标题暂缺"
        assert row["url"] == "https://openalex.org/W6929328961"

    def test_title_search_is_marked_as_search(self):
        row = relation_link({"dst_id": "sciverse:x", "dst_title": "Unresolved"},
                            local_lookup=lambda pid: None)
        assert row["search_link"] is True and row["link_kind"] == "search"

    def test_unsafe_url_is_ignored(self):
        row = relation_link({"dst_id": "sciverse:x", "dst_url": "javascript:alert(1)"},
                            local_lookup=lambda pid: None)
        assert not str(row["url"]).startswith("javascript:")

    def test_labels_keep_the_entire_title_and_math_readable(self):
        label = short_label(r"$\mathrm{Gr}PO$ with a very long subtitle that keeps going and going")
        assert "$" not in label and "\\" not in label
        assert label == "GrPO with a very long subtitle that keeps going and going"


class TestGraphApi:
    @pytest.fixture()
    def client(self, config: AppConfig, db, pipeline_factory):
        from fastapi.testclient import TestClient

        from carrel.web import app as web_app

        application = web_app.create_app(config, db)
        pipeline = pipeline_factory()
        pdf = config.data_dir / "seed.pdf"
        pdf.write_bytes(b"%PDF-1.4 seed")
        paper_id = pipeline.process(str(pdf))
        return TestClient(application), paper_id

    def test_library_graph_scope_and_edges(self, client):
        c, pid = client
        data = c.get("/api/graph").json()
        assert data["scope"] == "library"
        assert data["stats"]["papers"] == 1
        ids = {n["id"] for n in data["nodes"]}
        for edge in data["edges"]:
            assert edge["source"] in ids and edge["target"] in ids

    def test_paper_graph_route_is_not_swallowed_by_catch_all(self, client):
        c, pid = client
        data = c.get(f"/api/papers/{pid}/graph").json()
        assert data["scope"] == "paper"
        assert data["focus"] == f"paper:{pid}"
        # The catch-all still answers with the paper record itself.
        assert c.get(f"/api/papers/{pid}").json()["id"] == pid

    def test_unknown_paper_is_404(self, client):
        c, _ = client
        r = c.get("/api/papers/nope/graph")
        assert r.status_code == 404 and r.json()["error_code"] == "not_found"

    def test_invalid_parameters_are_rejected(self, client):
        c, pid = client
        assert c.get("/api/graph?include_external=maybe").status_code == 422
        assert c.get("/api/graph?max_external=999").status_code == 422
        assert c.get(f"/api/papers/{pid}/graph?max_papers=13").status_code == 422
        assert c.get(f"/api/papers/{pid}/graph?max_external=999").status_code == 422

    def test_relations_page_serves_graph_without_relation_table(self, client):
        c, _ = client
        page = c.get("/relations").text
        assert 'data-graph-explorer' in page
        assert 'id="graph-data"' not in page
        assert "graph.js" in page
        table_view = c.get("/relations?view=table").text
        assert "关系表" not in table_view
        assert 'data-graph-explorer' in table_view

    def test_paper_page_embeds_its_graph(self, client):
        c, pid = client
        page = c.get(f"/papers/{pid}").text
        assert 'data-graph-scope="paper"' in page
        assert "data-graph-payload" in page
        assert "graph.css" in page

    def test_graph_does_not_write(self, client):
        c, pid = client
        before = c.get(f"/api/papers/{pid}").json()
        c.get("/api/graph")
        c.get(f"/api/papers/{pid}/graph")
        after = c.get(f"/api/papers/{pid}").json()
        assert before["status"] == after["status"]
        assert before["summary"] == after["summary"]
