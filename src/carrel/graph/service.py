"""Glue between the database/files on disk and the pure graph builders.

Reading is cached per request; the classification files are the only per-paper
I/O, and they are memoised on (path, mtime, size) so repeated tab switches do not
re-read 400 JSON files.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from carrel.graph.build import (
    DEFAULT_CAPACITY,
    DEFAULT_SINGLE_MAX_PAPERS,
    build_library_graph,
    build_paper_graph,
    _paper_node,
    revision_of,
)
from carrel.graph.labels import paper_label
from carrel.graph.links import relation_link
from carrel.graph.model import Edge, Graph
from carrel.motivation.extract import FIELDS, normalize_motivation
from carrel.motivation.tree import RELATION_LABELS, branch_of, citation_pairs, month_of, validate_edges
from carrel.store.paths import paper_dir, read_json
from carrel.titles import project_title


class GraphService:
    """Read-only projection of one Carrel data directory."""

    def __init__(self, db: Any, data_dir: Path | str) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self._cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._lock = threading.Lock()
        from carrel.graph.explorer import Explorer
        self._explorer = Explorer(self)

    def explore(self, **kwargs):
        return self._explorer.explore(**kwargs)

    def node_detail(self, paper_id):
        return self._explorer.node_detail(paper_id)

    def locate(self, q, **kwargs):
        return self._explorer.locate(q, **kwargs)

    def citation_overlay(self, paper_id, **kwargs):
        return self._explorer.citation_overlay(paper_id, **kwargs)

    def save_branch_name(self, branch_id, label):
        from carrel.graph.display import save_branch_name
        saved = save_branch_name(self.data_dir, branch_id, label)
        self.invalidate()
        return saved

    def refresh(self):
        self.invalidate()
        return self._explorer.refresh()

    def close(self):
        self._explorer.close()

    # ------------------------------------------------------------- reading
    def categories_for(self, paper: dict[str, Any]) -> list[dict[str, Any]]:
        path = paper_dir(self.data_dir, paper["id"]) / "classification.json"
        data = self._read_json_cached(path)
        categories = data.get("categories") if isinstance(data, dict) else None
        from carrel.classify.taxonomy import Taxonomy, project_path
        renames = Taxonomy.load(self.data_dir / 'taxonomy.toml', fallback=False).renames
        raw = [c for c in categories if isinstance(c, dict)] if isinstance(categories, list) else []
        if not raw and paper.get('discipline'):
            raw = [paper]
        projected = {}
        primary = data.get('primary_category_id') or (project_path(raw[0], renames)['category_id'] if raw else '')
        for category in raw:
            item = project_path(category, renames)
            item['primary'] = bool(category.get('primary') or (primary and primary == item['category_id']))
            previous = projected.get(item['category_id'])
            if previous:
                item['primary'] |= previous['primary']
            projected[item['category_id']] = item
        result = sorted(projected.values(), key=lambda c: (
            c['category_id'] != primary if primary else True, not c['primary'], c['category_id']))
        for index, item in enumerate(result):
            item['primary'] = index == 0
        return result

    def _read_json_cached(self, path: Path) -> dict[str, Any]:
        try:
            stat = path.stat()
        except OSError:
            return {}
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
        key = str(path)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == stamp:
                return cached[1]
        data = read_json(path, {}) or {}
        if not isinstance(data, dict):
            data = {}
        with self._lock:
            self._cache[key] = (stamp, data)
        return data

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
        self._explorer.invalidate()

    def papers(self) -> list[dict[str, Any]]:
        from carrel.graph.requirements import readiness
        eligible = set(readiness(self.db, self.data_dir, 'library', costs=False)['eligible'])
        return [project_title(p, paper_dir(self.data_dir, p['id']))
                for p in self.db.list_papers() if p['id'] in eligible]

    def _display_names(self, graph: Graph) -> Graph:
        for node in graph.nodes:
            if node.role != "paper":
                continue
            pid = node.meta["paper_id"]
            glossary = self._read_json_cached(paper_dir(self.data_dir, pid) / "glossary.json")
            summary = self._read_json_cached(paper_dir(self.data_dir, pid) / "summary.json")
            node.label = paper_label(node.meta.get("title_original", ""), node.title, glossary, summary)
            manual = self._read_json_cached(paper_dir(self.data_dir, pid) / "graph_display.json")
            node.label = str(manual.get("label") or node.label)
            if not node.meta.get("branch"):
                node.meta["branch"] = branch_of({"id": pid, **node.meta}, self.categories_for({"id": pid}))
        graph.revision = revision_of([graph.revision, [(n.id, n.label, n.meta.get("branch")) for n in graph.nodes]])
        return graph

    # ------------------------------------------------------------- graphs
    def library_graph(
        self,
        *,
        include_external: bool = False,
        max_external: int = DEFAULT_CAPACITY["external"],
        discipline: str = "",
        field: str = "",
        subfield: str = "",
    ) -> Graph:
        papers = self.papers()
        rows = self.db.tag_rows()
        relations = [dict(r) for r in self.db.query(
            "SELECT * FROM relations ORDER BY src_id, kind, rank, dst_id")]
        categories = {p["id"]: self.categories_for(p) for p in papers}

        def categories_for(paper: dict[str, Any]) -> list[dict[str, Any]]:
            return categories.get(paper["id"]) or []

        if discipline or field or subfield:
            papers = [p for p in papers if _matches(categories_for(p), discipline,
                                                    field, subfield)]
        graph = build_library_graph(
            papers=papers,
            eligible_ids={p['id'] for p in papers},
            categories_for=categories_for,
            tags=rows,
            relations=relations,
            link_resolver=self.link_resolver,
            include_external=include_external,
            max_external=max_external,
        )
        return self._display_names(graph)

    def paper_graph(
        self,
        paper_id: str,
        *,
        include_external: bool = True,
        max_papers: int = DEFAULT_SINGLE_MAX_PAPERS,
        max_external: int | None = None,
    ) -> Graph | None:
        paper = self.db.get_paper(paper_id)
        if not paper:
            return None
        paper = project_title(paper, paper_dir(self.data_dir, paper_id))
        by_id = {p["id"]: p for p in self.papers()}
        if paper_id not in by_id:
            return Graph(scope='paper', focus=paper_id, revision='unclassified')
        graph = build_paper_graph(
            paper=paper,
            eligible_ids=set(by_id),
            categories=self.categories_for(paper),
            tags=self.db.tags_for(paper_id),
            relations=[dict(r) for r in self.db.query(
                "SELECT * FROM relations ORDER BY src_id, kind, rank, dst_id")],
            papers_by_id=by_id,
            categories_by_id={pid: self.categories_for(p) for pid, p in by_id.items()},
            library_tags=self.db.tag_rows(),
            link_resolver=self.link_resolver,
            include_external=include_external,
            max_papers=max_papers,
            max_external=max_external,
        )
        return self._display_names(graph)

    # ------------------------------------------------------ motivation tree
    def motivation_tree(self, focus: str = "") -> Graph:
        saved = self._read_json_cached(self.data_dir / "motivation_tree.json")
        graph = Graph(scope="motivation_tree", revision=str(saved.get("revision") or ""))
        graph.notes = list(saved.get("notes") or [])
        graph.stats = {**(saved.get("stats") or {}), "papers": 0, "edges": 0, "branches": 0, "isolated": 0,
                       "generated_at": saved.get("generated_at", "")}
        if not saved:
            graph.notes.append('尚未梳理动机脉络，点击“重新梳理”生成。')
            return graph
        from carrel.graph.requirements import readiness
        eligible = set(readiness(self.db, self.data_dir, 'motivation_tree', costs=False)['eligible'])
        current = {p['id']: project_title(p, paper_dir(self.data_dir, p['id']))
                   for p in self.db.list_papers() if p['id'] in eligible}
        participants = set(saved.get("paper_ids") or [])
        if "paper_ids" not in saved:
            participants = set(current)
        nodes = {}
        for pid in sorted(participants & current.keys()):
            motivation = self._read_json_cached(paper_dir(self.data_dir, pid) / "motivation.json")
            if not normalize_motivation(motivation):
                graph.notes.append(f"《{current[pid].get('title') or pid}》缺少动机，请重新梳理。")
                continue
            node = _paper_node(current[pid], graph.revision)
            node.meta.update({key: motivation.get(key) for key in FIELDS})
            node.meta["origin"] = "llm"
            node.meta["month"] = month_of(current[pid])
            node.meta["branch"] = branch_of(current[pid], self.categories_for(current[pid]))
            nodes[pid] = node
        if participants - current.keys():
            graph.notes.append("已过滤被移出论文库的节点及关系；可重新梳理更新脉络。")
        citations = citation_pairs(self.db)
        edges, notes = validate_edges(saved.get("edges", []),
                                      {pid: current[pid] for pid in nodes}, citations)
        graph.notes = list(dict.fromkeys(graph.notes + notes))
        # The canvas is a readable research spine: each paper gets one main
        # predecessor. Keep the other validated edges for the selected paper's
        # detail panel, but mark them as supporting so the browser can hide them
        # from the mainline layout.
        primary_by_target: dict[str, dict] = {}
        for edge in edges:
            previous = primary_by_target.get(edge["to"])
            if previous is None or (
                float(edge["confidence"]), edge["basis"] == "citation", edge["from"]
            ) > (
                float(previous["confidence"]), previous["basis"] == "citation", previous["from"]
            ):
                primary_by_target[edge["to"]] = edge
        primary_count = 0
        for edge in edges:
            is_primary = primary_by_target.get(edge["to"]) is edge
            primary_count += int(is_primary)
            graph.edges.append(Edge(
                source=nodes[edge["from"]].id, target=nodes[edge["to"]].id, role="motivation",
                label=RELATION_LABELS[edge["relation"]], evidence=edge["evidence"],
                basis=edge["basis"], primary=is_primary))
        connected = {pid for edge in edges for pid in (edge["from"], edge["to"])}
        graph.nodes = [node for pid, node in nodes.items() if pid in connected]
        if focus in connected:
            graph.focus = nodes[focus].id
        graph.stats.update(papers=len(connected), edges=primary_count,
                           supporting_edges=max(0, len(edges) - primary_count),
                           isolated=len(nodes) - len(connected),
                           branches=len({nodes[pid].meta["branch"]["key"] for pid in connected}))
        return self._display_names(graph)

    def motivation_neighbors(self, paper_id: str) -> dict[str, list]:
        graph = self.motivation_tree()
        nodes = {node.id: node for node in graph.nodes}
        result = {"upstream": [], "downstream": []}
        for edge in graph.edges:
            source, target = nodes[edge.source], nodes[edge.target]
            if source.meta["paper_id"] == paper_id:
                side, other = "downstream", target
            elif target.meta["paper_id"] == paper_id:
                side, other = "upstream", source
            else:
                continue
            result[side].append({"id": other.meta["paper_id"], "title": other.title,
                                 "year": other.meta["year"], "month": other.meta["month"],
                                 "relation": edge.label, "basis": edge.basis, "evidence": edge.evidence})
        return result

    # ------------------------------------------------------------- links
    def link_resolver(self, record: dict[str, Any]) -> dict[str, Any]:
        def lookup(pid):
            row = self.db.get_paper(pid)
            return project_title(row, paper_dir(self.data_dir, pid)) if row else None
        return relation_link(record, local_lookup=lookup)


def _matches(categories: list[dict[str, Any]], discipline: str, field: str,
             subfield: str) -> bool:
    for category in categories:
        if ((not discipline or category.get("discipline") == discipline)
                and (not field or category.get("field") == field)
                and (not subfield or category.get("subfield") == subfield)):
            return True
    return False
