"""Read-only graph construction.

The library graph projects the SQLite tables and the per-paper classification
files into nodes and edges. Nothing here writes, and the output order is stable:
the same data always produces the same node list, whatever the insert order was.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Iterable

from carrel.graph.identity import (
    AliasIndex,
    ExternalRegistry,
    category_id,
    category_key,
    normalize_arxiv,
    normalize_doi,
    openalex_id,
    paper_node_id,
    tag_id,
)
from carrel.graph.model import Edge, Graph, Node
from carrel.graph.links import display_label
from carrel.titles import display_title

READABLE_STATUSES = ("ready", "ready_with_warning", "partial")
EXTERNAL_KINDS = ("cites", "cited_by", "similar")
DEFAULT_SINGLE_MAX_PAPERS = 5
SINGLE_MAX_OPTIONS = (5, 12, 24, 40)
DEFAULT_CAPACITY = {"papers": 400, "tags": 150, "external": 40, "visible": 250}

# Which relation rows count as an explicit, navigable link in this library.
LinkResolver = Callable[[dict[str, Any]], dict[str, Any]]


def revision_of(payload: Any) -> str:
    """Content digest used to invalidate saved selection and preferences."""
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def short_label(title: str, limit: int | None = None) -> str:
    """Plain-text canvas label; the browser wraps the complete title."""
    text = " ".join(str(title or "").split())
    if not text:
        return "文献标题暂缺"
    text = re.sub(r"\\[a-zA-Z]+\{([^{}]*)\}", r"\1", text)  # \mathrm{Gr} → Gr
    text = text.replace("$", "").replace("\\", "")
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,.;:，。；：") + "…"


def _sorted_papers(papers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(paper: dict[str, Any]) -> tuple:
        return (
            display_title(paper).casefold(),
            str(paper.get("id") or ""),
        )

    return sorted(papers, key=key)


def paper_categories(paper: dict[str, Any], categories: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for category in categories:
        path = {k: str(category.get(k) or "").strip() for k in ("discipline", "field", "subfield")}
        if category.get('category_id'):
            path['category_id'] = category['category_id']
        if path["discipline"]:
            out.append(path)
    if out:
        return out
    fallback = {"discipline": str(paper.get("discipline") or "").strip(),
                "field": str(paper.get("field") or "").strip(), "subfield": ""}
    return [fallback] if fallback["discipline"] else []


def _category_label(path: dict[str, str]) -> str:
    return path.get("subfield") or path.get("field") or path.get("discipline") or "未分类"


def _node_sort_key(role: str, label: str, identifier: str) -> str:
    return f"{role}\u0000{label.casefold()}\u0000{identifier}"


def _paper_node(paper: dict[str, Any], revision: str, *, focus: bool = False) -> Node:
    title = display_title(paper)
    original = str(paper.get("title") or "")
    paper_id = str(paper["id"])
    return Node(
        id=paper_node_id(paper_id),
        role="paper",
        label=short_label(title),
        title=title,
        url=f"/papers/{paper_id}",
        link_kind="local",
        sort_key=_node_sort_key("paper", str(paper.get("title") or title), paper_id),
        focus=focus,
        meta={
            "paper_id": paper_id,
            "title_original": original,
            "year": paper.get("year"),
            "status": paper.get("status"),
            "discipline": paper.get("discipline") or "",
            "field": paper.get("field") or "",
        },
    )


def _category_node(path: dict[str, str], depth: int, parent: str) -> Node:
    identifier = category_id(path)
    return Node(
        id=identifier,
        role="category",
        label=_category_label(path),
        title=" / ".join(p for p in (path.get("discipline"), path.get("field"),
                                     path.get("subfield")) if p),
        parent=parent,
        sort_key=_node_sort_key("category", identifier, category_key(path)),
        meta={"path": {k: v for k, v in path.items() if v}, "depth": depth,
              "discipline": path.get("discipline", ""), "field": path.get("field", ""),
              "subfield": path.get("subfield", "")},
    )


def _ancestors(path: dict[str, str]) -> list[dict[str, str]]:
    """Every proper ancestor of a category path, shallowest first."""
    chain: list[dict[str, str]] = []
    if path.get("discipline"):
        chain.append({"discipline": path["discipline"], "field": "", "subfield": ""})
    if path.get("discipline") and path.get("field"):
        chain.append({"discipline": path["discipline"], "field": path["field"], "subfield": ""})
    return chain


def _chain(path: dict[str, str]) -> list[dict[str, str]]:
    """The path plus every ancestor, shallowest first: the tree spine for it."""
    return [*_ancestors(path), path]


def _register_category(nodes: dict[str, Node], path: dict[str, str]) -> str:
    """Add every level of ``path`` and return the leaf id.

    A level's parent is the level above it — never itself. Getting this wrong
    turns the tree into a set of self-parenting roots and collapses the layout.
    """
    chain = _chain(path)
    for depth, level in enumerate(chain):
        identifier = category_id(level)
        if identifier in nodes:
            continue
        parent = category_id(chain[depth - 1]) if depth else ""
        nodes[identifier] = _category_node(level, depth, parent)
    return category_id(chain[-1])


def _category_edges(nodes: dict[str, Node]) -> list[Edge]:
    return [Edge(source=node.parent, target=node.id, role="membership",
                 label="下级分类", evidence="分类层级")
            for node in nodes.values() if node.role == "category" and node.parent in nodes]


# ------------------------------------------------------------------ library


def build_library_graph(
    *,
    papers: Iterable[dict[str, Any]],
    categories_for: Callable[[dict[str, Any]], list[dict[str, Any]]],
    tags: Iterable[dict[str, Any]],
    relations: Iterable[dict[str, Any]],
    link_resolver: LinkResolver,
    include_external: bool = False,
    max_external: int = DEFAULT_CAPACITY["external"],
    capacity: dict[str, int] | None = None,
    eligible_ids: set[str] | None = None,
) -> Graph:
    """Whole-library graph: category backbone, local papers, shared tags.

    Papers outside ``READABLE_STATUSES`` are left to the jobs page. Only the
    first complete category becomes a paper's parent in the tree; the others stay
    as cross-membership edges so one paper is never duplicated.
    """
    limits = {**DEFAULT_CAPACITY, **(capacity or {})}
    local = _sorted_papers(p for p in papers if (p['id'] in eligible_ids if eligible_ids is not None
                                                else p.get("status") in READABLE_STATUSES))
    notes: list[str] = []

    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    category_nodes: dict[str, Node] = {}
    tag_nodes: dict[str, Node] = {}
    tag_members: dict[str, list[str]] = {}
    missing_levels = 0

    for paper in local[: limits["papers"]]:
        paper_id = str(paper["id"])
        node_id = paper_node_id(paper_id)
        nodes[node_id] = _paper_node(paper, revision_of(paper))
        paths = paper_categories(paper, categories_for(paper))
        if not paths:
            continue
        if not (categories_for(paper)):
            missing_levels += 1
        for index, path in enumerate(paths):
            leaf_id = _register_category(category_nodes, path)
            edges.append(Edge(source=node_id, target=leaf_id, role="membership",
                              primary=index == 0,
                              evidence="主分类" if index == 0 else "其他分类"))

    if len(local) > limits["papers"]:
        notes.append(f"资料库共 {len(local)} 篇论文，本次最多展示 {limits['papers']} 篇。")
    if missing_levels:
        notes.append(f"{missing_levels} 篇论文缺少完整分类文件，已使用两级分类字段兜底。")

    for node in category_nodes.values():
        nodes[node.id] = node
    edges.extend(_category_edges(category_nodes))

    # Shared tags only: a tag on a single paper is noise on the library canvas
    # and stays reachable from that paper's own graph.
    paper_ids = {str(p["id"]) for p in local}
    for row in tags:
        paper_id = str(row.get("paper_id") or "")
        label = str(row.get("tag") or "")
        if not label or paper_id not in paper_ids:
            continue
        tag_members.setdefault(label, []).append(paper_id)
    shared = sorted((label for label, ids in tag_members.items() if len(ids) > 1),
                    key=str.casefold)[: limits["tags"]]
    for label in shared:
        identifier = tag_id(label)
        tag_nodes[identifier] = Node(
            id=identifier, role="tag", label=label,
            title=label,
            sort_key=_node_sort_key("tag", label, identifier),
            meta={"tag": label, "count": len(tag_members[label])},
        )
        for paper_id in sorted(tag_members[label]):
            edges.append(Edge(source=paper_node_id(paper_id), target=identifier,
                              role="tag", label=label, evidence="共享标签"))
    nodes.update(tag_nodes)

    # Local relations: keep only rows whose both ends are library papers.
    alias = AliasIndex.from_papers(local)
    seen: dict[tuple[str, str, str], Edge] = {}
    external_rows: list[dict[str, Any]] = []
    for record in relations:
        src = str(record.get("src_id") or "")
        if src not in paper_ids:
            continue
        kind = str(record.get("kind") or "")
        if kind not in EXTERNAL_KINDS:
            continue
        target = alias.local_paper(record)
        if target == src:
            continue
        if target and target != src:
            source_id, target_id = (target, src) if kind == "cited_by" else (src, target)
            role = "similar" if kind == "similar" else "cites"
            a, b = sorted((source_id, target_id)) if role == "similar" else (source_id, target_id)
            key = (role, a, b)
            if key in seen:
                evidence = set(filter(None, seen[key].evidence.split(" · ")))
                evidence.add(str(record.get("provider") or ""))
                seen[key].evidence = " · ".join(sorted(filter(None, evidence)))
                continue
            edge = Edge(source=paper_node_id(a), target=paper_node_id(b),
                        role=role, label="相似" if role == "similar" else "引用",
                        evidence=str(record.get("provider") or ""))
            seen[key] = edge
            edges.append(edge)
            continue
        external_rows.append({**record, "src_id": src})

    # Similar edges are undirected: dedupe the two stored directions.
    deduped: list[Edge] = []
    for edge in edges:
        if edge.role != "similar":
            deduped.append(edge)
            continue
        pair = tuple(sorted((edge.source, edge.target)))
        if any(e.role == "similar" and tuple(sorted((e.source, e.target))) == pair
               for e in deduped):
            continue
        deduped.append(edge)
    edges = deduped

    stats: dict[str, Any] = {
        "papers": len(local),
        "categories": len(category_nodes),
        "tags": len(tag_nodes),
        "edges": len(edges),
        "external": {"shown": 0, "available": 0, "omitted": 0},
        "truncated": len(local) > limits["papers"],
    }
    if include_external and external_rows:
        limit = max(0, min(int(max_external or 0), limits["external"]))
        # The per-paper quota is bounded; the library-wide total is bounded too,
        # otherwise 400 papers × 40 works would swamp any canvas.
        budget = min(limit * max(1, len(local)), max(limit, limits["visible"] // 2))
        external_nodes, external_edges = _external_projection(
            external_rows, alias, link_resolver, limit, budget=budget,
            paper_id_for=lambda row: paper_node_id(str(row["src_id"])))
        nodes.update(external_nodes)
        edges.extend(external_edges)
        available = len(_external_groups(external_rows))
        stats["external"] = {
            "shown": len(external_nodes),
            "available": available,
            "omitted": max(0, available - len(external_nodes)),
        }
        if stats["external"]["omitted"]:
            notes.append(f"外部文献共 {available} 条，本次展示 {len(external_nodes)} 条。")
        stats["edges"] = len(edges)

    stats["visible"] = len(nodes)
    if stats["visible"] > limits["visible"]:
        notes.append(f"当前可见节点 {stats['visible']} 个，超过一次绘制的建议上限 "
                     f"{limits['visible']} 个，可收拢部分分类分支。")
    ordered = sorted(nodes.values(), key=lambda n: (_role_order(n.role), n.sort_key))
    return Graph(
        scope="library",
        revision=revision_of({
            "papers": [{"id": p["id"], "title": p.get("title"), "status": p.get("status"),
                        "discipline": p.get("discipline"), "field": p.get("field")}
                       for p in local],
            "categories": sorted(category_nodes),
            "tags": sorted(tag_nodes),
            "edges": sorted((e.source, e.target, e.role) for e in edges),
        }),
        nodes=ordered,
        edges=sorted(edges, key=lambda e: (e.role, e.source, e.target)),
        stats=stats,
        notes=notes,
    )


def _role_order(role: str) -> int:
    return {"category": 0, "paper": 1, "tag": 2, "external": 3}.get(role, 9)


def _external_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    registry = ExternalRegistry()
    for row in rows:
        registry.add(row)
    return registry.resolve()


def _external_projection(
    rows: list[dict[str, Any]],
    alias: AliasIndex,
    link_resolver: LinkResolver,
    limit: int,
    *,
    budget: int | None = None,
    paper_id_for: Callable[[dict[str, Any]], str],
) -> tuple[dict[str, Node], list[Edge]]:
    """Build external work nodes and the edges that reach them.

    ``limit`` bounds the number of external works per source paper, so one paper
    with 33 references does not hide every other paper's neighbourhood;
    ``budget`` bounds the total across the whole graph.
    """
    per_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        per_source.setdefault(paper_id_for(row), []).append(row)

    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    remaining = budget if budget is not None else None
    for source, source_rows in per_source.items():
        if remaining is not None and remaining <= 0:
            break
        registry = ExternalRegistry()
        for row in source_rows:
            registry.add(row)
        groups, row_keys = registry.resolve_grouped()
        ordered = sorted(groups.values(), key=lambda item: (
            item[0].kind, item[0].title.casefold(), item[0].key))
        take = min(max(0, limit), remaining if remaining is not None else limit)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row, key in zip(source_rows, row_keys, strict=False):
            by_key.setdefault(key, []).append(row)
        for entry, _records in ordered[:take]:
            rows_for_key = by_key.get(entry.key, [])
            display = link_resolver(rows_for_key[0]) if rows_for_key else {}
            node = Node(
                id=entry.key,
                role="external",
                label=short_label(entry.title),
                title=entry.title or "文献标题暂缺",
                url=display.get("url", ""),
                link_kind=display.get("link_kind", "unavailable"),
                sort_key=_node_sort_key("external", entry.title, entry.key),
                meta={
                    "external": True,
                    "year": entry.year,
                    "arxiv": entry.arxiv,
                    "doi": entry.doi,
                    "search_link": bool(display.get("search_link")),
                    "providers": sorted({str(r.get("provider") or "") for r in rows_for_key
                                         if r.get("provider")}),
                },
            )
            if node.id not in nodes:
                nodes[node.id] = node
                if remaining is not None:
                    remaining -= 1
            for row in rows_for_key:
                kind = str(row.get("kind") or "")
                edges.append(Edge(
                    source=node.id if kind == "cited_by" else paper_id_for(row),
                    target=paper_id_for(row) if kind == "cited_by" else node.id,
                    role="similar" if kind == "similar" else "cites",
                    label=relation_label(kind),
                    evidence=str(row.get("provider") or ""),
                ))
    return nodes, edges


def relation_label(kind: str) -> str:
    return {"cites": "引用", "cited_by": "被引用", "similar": "相似"}.get(kind, kind)


# -------------------------------------------------------------------- single


def build_paper_graph(
    *,
    paper: dict[str, Any],
    categories: list[dict[str, Any]],
    tags: Iterable[dict[str, Any]],
    relations: Iterable[dict[str, Any]],
    papers_by_id: dict[str, dict[str, Any]],
    link_resolver: LinkResolver,
    categories_by_id: dict[str, list[dict[str, Any]]] | None = None,
    library_tags: Iterable[dict[str, Any]] = (),
    include_external: bool = True,
    max_papers: int = DEFAULT_SINGLE_MAX_PAPERS,
    max_external: int | None = None,
    eligible_ids: set[str] | None = None,
) -> Graph:
    """A bounded neighbourhood, ranked by direct and shared library evidence."""
    paper_id = str(paper["id"])
    focus_id = paper_node_id(paper_id)
    nodes: dict[str, Node] = {focus_id: _paper_node(paper, "", focus=True)}
    edges: list[Edge] = []
    notes: list[str] = []

    paths = paper_categories(paper, categories)
    if not categories:
        notes.append("论文缺少分类文件，分类来自两级分类字段。")
    for path in paths:
        nodes[category_id(path)] = _category_node(path, 0, "")
        edges.append(Edge(source=focus_id, target=category_id(path), role="membership",
                          label="分类", evidence=" / ".join(
                              p for p in (path.get("discipline"), path.get("field"),
                                          path.get("subfield")) if p)))

    tag_rows = [dict(t) for t in tags if str(t.get("tag") or "").strip()]
    for row in sorted(tag_rows, key=lambda t: str(t["tag"]).casefold()):
        label = str(row["tag"])
        identifier = tag_id(label)
        nodes[identifier] = Node(
            id=identifier, role="tag", label=label, title=label,
            sort_key=_node_sort_key("tag", label, identifier),
            meta={"tag": label, "origin": row.get("origin") or "",
                  "evidence": row.get("evidence") or ""},
        )
        edges.append(Edge(source=focus_id, target=identifier, role="tag",
                          label=label, evidence=str(row.get("evidence") or "")))

    categories_by_id = categories_by_id or {}
    tags_by_id: dict[str, set[str]] = {}
    for row in library_tags:
        label = str(row.get("tag") or "").strip()
        if label:
            tags_by_id.setdefault(str(row.get("paper_id") or ""), set()).add(label)
    focus_tags = {str(t["tag"]) for t in tag_rows}
    alias = AliasIndex.from_papers(papers_by_id.values())
    external_registry = ExternalRegistry()
    direct: dict[str, list[dict[str, Any]]] = {}
    for record in relations:
        kind = str(record.get("kind") or "")
        if kind not in EXTERNAL_KINDS:
            continue
        source = str(record.get("src_id") or paper_id)
        target = alias.local_paper(record)
        if target == paper_id and source != paper_id:
            target = source
            kind = {"cites": "cited_by", "cited_by": "cites"}.get(kind, kind)
        elif source != paper_id or target == paper_id:
            continue
        row = {**record, "src_id": paper_id, "kind": kind}
        if target:
            if (target in eligible_ids if eligible_ids is not None else papers_by_id.get(target, {}).get("status") in READABLE_STATUSES):
                direct.setdefault(target, []).append(row)
        else:
            external_registry.add(row)

    local_candidates = []
    for candidate in _sorted_papers(papers_by_id.values()):
        pid = str(candidate["id"])
        if pid == paper_id or (pid not in eligible_ids if eligible_ids is not None else candidate.get("status") not in READABLE_STATUSES):
            continue
        shared_tags = sorted(focus_tags & tags_by_id.get(pid, set()), key=str.casefold)
        shared_paths = _shared_categories(paths, paper_categories(
            candidate, categories_by_id.get(pid, [])))
        records = direct.get(pid, [])
        if not (records or shared_tags or shared_paths):
            continue
        score = (bool(records), len(shared_tags),
                 max((bool(p.get("subfield")) for p in shared_paths), default=False),
                 len(shared_paths))
        local_candidates.append((score, candidate, shared_tags, shared_paths, records))
    # Python's stable sort preserves the title/id tie breaker from _sorted_papers.
    local_candidates.sort(key=lambda item: item[0], reverse=True)
    budget = max(0, min(int(max_papers), SINGLE_MAX_OPTIONS[-1]) - 1)
    for _, candidate, shared_tags, shared_paths, records in local_candidates[:budget]:
        node = _paper_node(candidate, "")
        reasons = sorted({relation_label(str(r["kind"])) for r in records})
        reasons.extend(f"共享标签：{tag}" for tag in shared_tags)
        reasons.extend(f"同{'子领域' if p.get('subfield') else '研究方向'}：{_category_label(p)}"
                       for p in shared_paths)
        node.meta["reasons"] = reasons
        nodes[node.id] = node
        edges.extend(_focus_relation_edges(focus_id, node.id, records))
        for tag in shared_tags:
            edges.append(Edge(source=node.id, target=tag_id(tag), role="tag",
                              label=tag, evidence="共享标签"))
        for path in shared_paths:
            nodes[category_id(path)] = _category_node(path, 0, "")
            if not any(e.source == focus_id and e.target == category_id(path) for e in edges):
                edges.append(Edge(source=focus_id, target=category_id(path), role="membership",
                                  label="研究方向", evidence="分类的共同上级"))
            edges.append(Edge(source=node.id, target=category_id(path), role="membership",
                              label="同研究方向", evidence=" / ".join(v for v in path.values() if v)))

    groups, _ = external_registry.resolve_grouped()
    external_candidates = []
    focus_words = _title_words(str(paper.get("title") or "") + " " + str(paper.get("title_zh") or ""))
    context_words = _title_words(" ".join([*focus_tags, *(_category_label(p) for p in paths)]))
    for entry, records in groups.values():
        title = display_label({"dst_title": entry.title}, None)
        if title == "文献标题暂缺":
            continue
        displays = [link_resolver(row) for row in records]
        display = min(displays, key=lambda d: (d.get("link_kind") != "source", not d.get("url")))
        if not display.get("url"):
            continue
        title_lower = title.casefold()
        matched_tags = sorted(tag for tag in focus_tags if tag.casefold() in title_lower)
        matched_words = sorted(focus_words & _title_words(title))
        context_matches = context_words & _title_words(title)
        rank = min((r.get("rank") if r.get("rank") is not None else 10000) for r in records)
        score = (-len(matched_tags), -len(context_matches), -len(matched_words), display.get("link_kind") != "source",
                 rank, title.casefold(), entry.key)
        external_candidates.append((score, entry, records, display, matched_tags, matched_words))
    external_candidates.sort(key=lambda item: item[0])
    remaining = max(0, budget - min(budget, len(local_candidates))) if include_external else 0
    if max_external is not None:
        remaining = min(remaining, max(0, max_external))
    for _, entry, records, display, matched_tags, matched_words in external_candidates[:remaining]:
        reasons = sorted({relation_label(str(r["kind"])) for r in records})
        reasons.extend(f"标题含标签：{tag}" for tag in matched_tags)
        if matched_words:
            reasons.append("标题关键词：" + "、".join(matched_words))
        nodes[entry.key] = Node(
            id=entry.key, role="external", label=short_label(entry.title), title=entry.title,
            url=display["url"], link_kind=display.get("link_kind", "source"),
            sort_key=_node_sort_key("external", entry.title, entry.key),
            meta={"external": True, "year": entry.year, "arxiv": entry.arxiv, "doi": entry.doi,
                  "search_link": bool(display.get("search_link")), "reasons": reasons},
        )
        edges.extend(_focus_relation_edges(focus_id, entry.key, records))
        for tag in matched_tags:
            edges.append(Edge(source=entry.key, target=tag_id(tag), role="tag",
                              label="标题含标签", evidence=tag))
    # Context shared with a neighbour earns its space; keep a few personal tags
    # when the library has no shared tags yet. All tags remain on the paper page.
    shared_tag_ids = {e.target for e in edges if e.role == "tag" and e.source != focus_id}
    keep_tags = shared_tag_ids or {tag_id(t) for t in sorted(focus_tags, key=str.casefold)[:3]}
    nodes = {key: node for key, node in nodes.items() if node.role != "tag" or key in keep_tags}
    edges = [e for e in edges if e.source in nodes and e.target in nodes]
    edges.extend(_category_edges(nodes))
    shown = sum(n.role == "external" for n in nodes.values())
    available = len(external_candidates)
    local_shown = sum(n.role == "paper" for n in nodes.values())

    node_list = sorted(nodes.values(), key=lambda n: (
        0 if n.focus else 1 + _role_order(n.role), n.sort_key))
    return Graph(
        scope="paper",
        focus=focus_id,
        revision=revision_of({
            "nodes": [n.to_dict() for n in node_list],
            "edges": sorted((e.source, e.target, e.role) for e in edges),
        }),
        nodes=node_list,
        edges=sorted(edges, key=lambda e: (e.role, e.source, e.target)),
        stats={
            "papers": local_shown,
            "selection": {"limit": max_papers, "shown": local_shown + shown,
                          "available": 1 + len(local_candidates) + (available if include_external else 0),
                          "local_available": len(local_candidates),
                          "filtered": len(groups) - available},
            "categories": sum(1 for n in node_list if n.role == "category"),
            "tags": sum(1 for n in node_list if n.role == "tag"),
            "edges": len(edges),
            "external": {"shown": shown, "available": available,
                         "omitted": max(0, available - shown)},
        },
        notes=notes,
    )


def _shared_categories(left: list[dict[str, str]], right: list[dict[str, str]]) -> list[dict[str, str]]:
    shared = {}
    for a in left:
        for b in right:
            if not a.get("field") or any(a.get(k) != b.get(k) for k in ("discipline", "field")):
                continue
            path = {"discipline": a["discipline"], "field": a["field"],
                    "subfield": a.get("subfield", "") if a.get("subfield") == b.get("subfield") else ""}
            shared[category_key(path)] = path
    # A shared subfield already carries its parent field.
    paths = list(shared.values())
    return sorted((p for p in paths if p.get("subfield") or not any(
        q.get("subfield") and q["discipline"] == p["discipline"] and q["field"] == p["field"]
        for q in paths)), key=category_key)


def _focus_relation_edges(focus: str, target: str, records: list[dict[str, Any]]) -> list[Edge]:
    edges = {}
    for row in records:
        kind = str(row["kind"])
        a, b = (target, focus) if kind == "cited_by" else (focus, target)
        role = "similar" if kind == "similar" else "cites"
        edges.setdefault((a, b, role), Edge(source=a, target=b, role=role,
                                           label=relation_label(kind), evidence=str(row.get("provider") or "")))
    return list(edges.values())


def _title_words(title: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "that", "this", "their",
            "are", "into", "using", "based", "towards", "study", "through",
            "anything", "general-purpose", "system", "systems"}
    return {word for word in re.findall(r"[a-z][a-z0-9-]{2,}", title.casefold()) if word not in stop}


# ---------------------------------------------------------------- utilities


def local_relation_rows(relations: Iterable[dict[str, Any]],
                        alias: AliasIndex) -> list[dict[str, Any]]:
    """Relation rows whose destination resolves to a library paper."""
    out: list[dict[str, Any]] = []
    for record in relations:
        target = alias.local_paper(record)
        if target:
            out.append({**record, "local_target": target})
    return out


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_SINGLE_MAX_PAPERS",
    "READABLE_STATUSES",
    "SINGLE_MAX_OPTIONS",
    "build_library_graph",
    "build_paper_graph",
    "local_relation_rows",
    "paper_categories",
    "revision_of",
    "short_label",
]
