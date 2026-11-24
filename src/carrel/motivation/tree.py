"""Build a library-only, time-directed DAG of research motivations."""
from __future__ import annotations

import json
import math
import re
import time

from carrel.model import ModelSchemaError, ModelTruncated
from carrel.model.base import call_json, pick
from carrel.graph.build import paper_categories, revision_of
from carrel.graph.identity import normalize_arxiv, normalize_doi
from carrel.motivation.extract import FIELDS
from carrel.store.paths import atomic_write_json, paper_dir, read_json
from carrel.store.runs import record_run

LIBRARY_ID = "@library"
BATCH_SIZE = 10
RELATION_LABELS = {"extends": "延续", "addresses_limitation": "针对局限", "contrasts": "对立"}
TREE_SCHEMA = {
    "type": "object",
    "properties": {"edges": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "from": {"type": "string"}, "to": {"type": "string"},
            "relation": {"type": "string", "enum": list(RELATION_LABELS)},
            "confidence": {"type": "number"}, "cited": {"type": "boolean"},
            "evidence": {"type": "string"},
        },
        "required": ["from", "to", "relation", "confidence", "cited", "evidence"],
        "additionalProperties": False,
    }}},
    "required": ["edges"], "additionalProperties": False,
}
SYSTEM = """根据库内论文的主要动机梳理研究脉络，只使用输入材料。
from 是较早的工作，to 是承接它的工作，箭头方向为先前工作 → 后续工作。
cites_pairs 的 [A,B] 表示 A 引用 B，因此对应脉络通常是 B → A。
优先考察真实库内引用骨架；但引用本身不等于动机递进，必须有具体语义证据。
也允许无引用但动机明显延续的边，将 cited 设为 false，不能用相同标签强行连边。
只保留有把握的关系：extends=延续，addresses_limitation=针对先前局限，
contrasts=针对同一问题提出对立思路。每条 evidence 用中文具体指出两项动机的联系。
confidence 取 0–1；纯语义边只保留置信度至少 0.75 的判断。宁可稀疏或为空，
不要为了连通而编造关系。节点只能用给定 id；同年可递进，未知年份不可猜测。
new_papers 是本批需要接入的论文，candidates 是已有脉络中的相关候选。
每条新边必须涉及至少一篇 new_papers；候选间已有关系无需重复。
month 是 arXiv 编号中的提交月份，不代表正式发表时间。
避免循环，每项工作通常最多两个主要前驱，跨方向只保留主要锚边。
只返回含 edges 的 JSON 对象。"""


def month_of(paper: dict) -> str:
    pid = str(paper.get("id") or "")
    if not pid.startswith("arxiv:"):
        return ""
    match = re.search(r"(?:^|/)(\d{2})(\d{2})[.\d]", pid[6:])
    if not match or not 1 <= int(match[2]) <= 12:
        return ""
    year = int(match[1])
    return f"{1900 + year if year >= 91 else 2000 + year}-{match[2]}"


def branch_of(paper, categories=()):
    from carrel.classify.taxonomy import project_path
    paths = paper_categories(paper, categories)
    primary_id = paper.get('primary_category_id')
    path = next((path for path in paths if path.get('category_id') == primary_id), paths[0] if paths else {})
    path = project_path(path)
    values = [path.get(k) for k in ("discipline", "field") if path.get(k)]
    return {"key": path.get('category_id') if values else "未分类",
            "label": values[-1] if values else "未分类",
            "parent": path.get('discipline', '')}


def citation_pairs(db):
    """Resolve direct IDs, late imports' identifiers, and reversed cited_by rows."""
    papers = {p["id"]: p for p in db.list_papers()
              if p.get("status") in ("ready", "ready_with_warning", "partial")}
    arxiv = {normalize_arxiv(pid[6:]): pid for pid in papers if pid.startswith("arxiv:")}
    doi = {normalize_doi(pid[4:]): pid for pid in papers if pid.startswith("doi:")}
    pairs = set()
    for row in db.query("SELECT * FROM relations WHERE kind IN ('cites','cited_by')"):
        source = row["src_id"]
        target = row["dst_id"] if row["dst_id"] in papers else (
            arxiv.get(normalize_arxiv(row["dst_arxiv"] or ""))
            or doi.get(normalize_doi(row["dst_doi"] or "")))
        if source not in papers or not target or source == target:
            continue
        if row["kind"] == "cites":
            pairs.add((source, target))
        else:
            pairs.add((target, source))
    return pairs


def library_revision(config, db):
    """Completed papers only; in-flight updated_at churn cannot trigger loops."""
    from carrel.motivation.extract import motivation_inputs

    from carrel.graph.requirements import readiness
    eligible = set(readiness(db, config.data_dir, 'motivation_tree', costs=False)['eligible'])
    rows = []
    for paper in db.list_papers():
        if paper['id'] not in eligible:
            continue
        base = paper_dir(config.data_dir, paper["id"])
        rows.append((paper["id"], month_of(paper), motivation_inputs(base)[1]))
    ids = {row[0] for row in rows}
    cites = sorted(pair for pair in citation_pairs(db) if pair[0] in ids and pair[1] in ids)
    return revision_of({"papers": sorted(rows), "cites": cites})


def _cycle(edges):
    """An iterative DFS returns one directed cycle without recursion limits."""
    adjacency = {}
    for i, edge in enumerate(edges):
        adjacency.setdefault(edge["from"], []).append((edge["to"], i))
    colors, parent = {}, {}
    for root in sorted(adjacency):
        if colors.get(root):
            continue
        colors[root] = 1
        stack = [(root, iter(adjacency.get(root, [])))]
        while stack:
            node, children = stack[-1]
            item = next(children, None)
            if item is None:
                colors[node] = 2
                stack.pop()
                continue
            child, index = item
            if colors.get(child) == 1:
                cycle, cursor = [index], node
                while cursor != child:
                    cursor, prior = parent[cursor]
                    cycle.append(prior)
                return cycle
            if not colors.get(child):
                colors[child] = 1
                parent[child] = (node, index)
                stack.append((child, iter(adjacency.get(child, []))))
    return []


def validate_edges(raw, papers_by_id, cites_pairs):
    """Return canonical edges and an explanation for every rejection/correction."""
    if isinstance(raw, dict):
        raw = raw.get("edges", [])
    if not isinstance(raw, list):
        return [], ["边表不是列表，已忽略。"]
    notes, selected = [], {}
    citations = set(cites_pairs)
    for index, item in enumerate(raw):
        prefix = f"第 {index + 1} 条关系"
        if not isinstance(item, dict):
            notes.append(f"{prefix}格式无效，已丢弃。")
            continue
        source, target = pick(item, "from", "source"), pick(item, "to", "target")
        if not isinstance(source, str) or not isinstance(target, str) \
                or source not in papers_by_id or target not in papers_by_id:
            notes.append(f"{prefix}含非库内端点，已丢弃。")
            continue
        prefix = f"{source} → {target}"
        if source == target:
            notes.append(f"{prefix}是自环，已丢弃。")
            continue
        first, second = month_of(papers_by_id[source]), month_of(papers_by_id[target])
        if first and second and first > second:
            notes.append(f"{prefix}与 arXiv 提交月份顺序相反，保留模型判断，请核对证据。")
        relation = item.get("relation")
        relation = {v: k for k, v in RELATION_LABELS.items()}.get(str(relation), relation)
        evidence = item.get("evidence")
        if not isinstance(relation, str) or relation not in RELATION_LABELS \
                or not isinstance(evidence, str) or not evidence.strip():
            notes.append(f"{prefix}缺少有效递进类型或证据句，已丢弃。")
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = float("nan")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            notes.append(f"{prefix}置信度无效，已丢弃。")
            continue
        supported = (target, source) in citations
        claimed = item.get("cited") is True or str(item.get("cited")).lower() in ("true", "1", "是")
        cited = claimed and supported
        if claimed and not supported:
            notes.append(f"{prefix}没有对应方向的库内引用记录，降为纯语义判断。")
        if not first or not second:
            notes.append(f"{prefix}提交月份未知，保留关系但无法核验时间顺序。")
        edge = {"from": source, "to": target, "relation": relation,
                "confidence": confidence, "cited": cited,
                "basis": "citation" if cited else "semantic", "evidence": evidence.strip()}
        key = (source, target)
        if key in selected:
            notes.append(f"{prefix}重复，保留置信度较高的一条。")
            if selected[key]["confidence"] >= confidence:
                continue
        selected[key] = edge
    edges = [selected[key] for key in sorted(selected)]
    while cycle := _cycle(edges):
        index = min(cycle, key=lambda i: (edges[i]["confidence"], edges[i]["from"], edges[i]["to"]))
        edge = edges.pop(index)
        notes.append(f"{edge['from']} → {edge['to']}形成循环，已丢弃环内置信度最低的边。")
    return edges, notes


def _compact(paper):
    return {"id": paper["id"], "month": paper["month"],
            "motivation": paper["motivation"][:300], "gap": paper["gap"][:220],
            "direction": paper["direction"][:80], "keywords": paper["keywords"][:6]}


def _candidates(batch, previous, cites):
    """Bound prompt size; prefer citations, then shared direction/keywords."""
    ids = {p["id"] for p in batch}
    keywords = {k.casefold() for p in batch for k in p["keywords"]}
    branches = {p["branch"] for p in batch}
    def score(paper):
        linked = any((paper["id"], pid) in cites or (pid, paper["id"]) in cites for pid in ids)
        overlap = len(keywords & {k.casefold() for k in paper["keywords"]})
        return (-int(linked), -overlap, -int(paper["branch"] in branches), paper["month"], paper["id"])
    ranked = sorted(previous, key=score)
    # Small libraries send all prior motivations. Large libraries stay below a
    # fixed prompt budget, independent of the total library size.
    budget = 14000 - len(json.dumps([_compact(p) for p in batch], ensure_ascii=False))
    selected = []
    for paper in ranked[:20]:
        cost = len(json.dumps(_compact(paper), ensure_ascii=False))
        if cost > budget:
            break
        selected.append(paper)
        budget -= cost
    return selected


def _build(config, db, backend, *, full=False, new_paper_ids=(), retry=False):
    path = config.data_dir / "motivation_tree.json"
    saved = read_json(path, {}) or {}
    from carrel.graph.requirements import readiness
    from carrel.motivation.extract import reusable_motivation
    ready = readiness(db, config.data_dir, 'motivation_tree', costs=False)
    eligible = set(ready['eligible'])
    excluded = [{'id': p['paper_id'], 'lacks': p['lacks']} for p in ready['missing']]
    papers = [p for p in db.list_papers() if p['id'] in eligible]
    material, notes = [], []
    for paper in sorted(papers, key=lambda p: (month_of(p) or "9999", p["id"])):
        motivation = reusable_motivation(paper_dir(config.data_dir, paper['id']))
        if not motivation:
            excluded.append({'id': paper['id'], 'lacks': ['motivation']})
            continue
        classification = read_json(paper_dir(config.data_dir, paper["id"]) / "classification.json", {}) or {}
        material.append({"id": paper["id"], "month": month_of(paper),
                         "branch": branch_of(paper, classification.get("categories") or [])["key"],
                         "input_fingerprint": motivation["input_fingerprint"],
                         **{key: motivation.get(key) for key in FIELDS}})
    by_id = {p["id"]: p for p in material}
    cites = {pair for pair in citation_pairs(db) if pair[0] in by_id and pair[1] in by_id}
    fingerprints = {p["id"]: revision_of(p) for p in material}
    revision = revision_of({"papers": material, "cites": sorted(cites)})
    if not full and saved.get("revision") == revision and not notes:
        if saved.get('excluded') != excluded:
            saved['excluded'] = excluded
            atomic_write_json(path, saved)
        return saved
    changed = set(by_id) if full else {pid for pid in by_id
                if (saved.get("paper_fingerprints") or {}).get(pid) != fingerprints[pid]}
    old_ids = set(saved.get("paper_ids") or [])
    changed |= (set(new_paper_ids) & set(by_id)) - old_ids
    # A new paper's citations are handled by its one incremental call. Only
    # citation changes between existing papers need to revisit old endpoints.
    prior_cites = {tuple(pair) for pair in saved.get("cites_pairs", [])}
    for source, target in cites ^ prior_cites:
        if source in old_ids and target in old_ids:
            changed.update(pid for pid in (source, target) if pid in by_id)
    pending = [p for p in material if p["id"] in changed]
    previous = [p for p in material if p["id"] not in changed]
    raw = [] if full else [e for e in saved.get("edges", [])
                          if e.get("from") in by_id and e.get("to") in by_id
                          and e["from"] not in changed and e["to"] not in changed]
    runs, candidate_omitted, rejected = [], 0, 0
    size = BATCH_SIZE
    for offset in range(0, len(pending), size):
        batch = pending[offset:offset + size]
        candidates = _candidates(batch, previous, cites)
        candidate_omitted += len(previous) - len(candidates)
        allowed = {p["id"] for p in batch + candidates}
        new_ids = {p["id"] for p in batch}
        if len(allowed) > 1:
            inputs = {"new_papers": [_compact(p) for p in batch],
                      "candidates": [_compact(p) for p in candidates],
                      "cites_pairs": sorted((a, b) for a, b in cites if a in allowed and b in allowed
                                            and (a in new_ids or b in new_ids))}
            reply = None
            try:
                reply = call_json(backend, schema=TREE_SCHEMA, system=SYSTEM,
                                  user=json.dumps(inputs, ensure_ascii=False),
                                  max_tokens=12000, task="motivation_tree")
                if reply.truncated:
                    raise ModelTruncated("脉络回复被截断，请提高 motivation_tree 的 max_tokens 后重试", reply=reply)
                if not isinstance(reply.data, dict) or not isinstance(reply.data.get("edges"), list):
                    raise ModelSchemaError("脉络回复缺少有效 edges 列表", reply=reply)
                for edge in reply.data["edges"]:
                    if isinstance(edge, dict) and isinstance(edge.get("from"), str) and isinstance(edge.get("to"), str) \
                            and edge["from"] in allowed and edge["to"] in allowed \
                            and (edge["from"] in new_ids or edge["to"] in new_ids):
                        raw.append(edge)
                    else:
                        notes.append("已丢弃不涉及本批论文或格式无效的关系。")
                        rejected += 1
                runs.append(record_run(config, db, backend, paper_id=LIBRARY_ID, task="motivation_tree",
                                       module=f"batch-{offset // size + 1}", meta=reply.accounting()))
            except Exception as exc:
                partial = getattr(exc, "reply", None) or reply
                record_run(config, db, backend, paper_id=LIBRARY_ID, task="motivation_tree",
                           status="failed", schema_ok=0, error_code=str(exc)[:200],
                           meta=partial.accounting() if partial else {})
                raise
        previous.extend(batch)
    edges, validation_notes = validate_edges(raw, by_id, cites)
    linked = {pid for e in edges for pid in (e["from"], e["to"])}
    if len(material) < 2:
        notes.append("至少需要两篇已提取动机的论文，才能梳理递进关系。")
    elif not edges:
        notes.append("当前材料未发现可靠的动机递进关系，保留各篇独立动机。")
    if any(pid.startswith("pdf-") for pid in by_id):
        notes.append("部分本地 PDF 缺少 arXiv / DOI 标识，引用匹配可能不完整；其联系主要依赖语义判断。")
    if candidate_omitted:
        notes.append("为控制单次上下文，已优先选择引用相关、关键词或分类接近的候选。")
    result = {"schema_version": 1, "revision": revision,
              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "origin": "llm", "paper_ids": sorted(by_id), "paper_fingerprints": fingerprints,
              "cites_pairs": sorted(cites), 'excluded': excluded,
              "edges": edges, "notes": notes + validation_notes, "run_ids": runs,
              "stats": {"papers_with_motivation": len(material), "papers_skipped": len(excluded),
                        "isolated": len(set(by_id) - linked), "edges_dropped": rejected + len(raw) - len(edges),
                        "branches": len({by_id[pid]["branch"] for pid in linked}), "batches": len(runs)}}
    atomic_write_json(path, result)
    return result


def extend_tree(config, db, backend, new_paper_ids=()):
    return _build(config, db, backend, new_paper_ids=new_paper_ids)


def rebuild_tree(config, db, backend):
    return _build(config, db, backend, full=True, retry=True)
