"""Value assessment.

This is the only place the model is asked for an opinion rather than a fact. It
is kept in its own file, tagged ``origin=llm``, traceable to a model run, and
optional — a paper reaches ``ready`` without it.
"""

from __future__ import annotations

import re
from typing import Any

from carrel.model import ModelBackend, ModelError
from carrel.model.base import call_json
from carrel.models import ModuleExtract, Summary

ASSESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevance": {
            "type": "object",
            "properties": {
                "to_library": {"type": "number"},
                "neighbors": {"type": "array", "items": {"type": "string"}},
                "to_projects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string"},
                            "score": {"type": "number"},
                            "why": {"type": "string"},
                        },
                        "required": ["project", "score", "why"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["to_library", "neighbors", "to_projects"],
            "additionalProperties": False,
        },
        "value": {
            "type": "object",
            "properties": {
                "novelty": {"type": "string"},
                "evidence_strength": {"type": "string"},
                "reproducibility": {"type": "string"},
                "read_priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "why": {"type": "string"},
            },
            "required": ["novelty", "evidence_strength", "reproducibility",
                         "read_priority", "why"],
            "additionalProperties": False,
        },
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevance", "value", "caveats"],
    "additionalProperties": False,
}

SYSTEM = """你在评估一篇论文对特定读者阅读库的价值。规则：
1. 只依据给出的分节要点与摘要，不要引入外部知识。
2. novelty / evidence_strength / reproducibility 各用一句话说明，并点出依据。
3. read_priority 只能取 high / medium / low。
4. caveats 写清结论成立的条件或你判断的不确定之处。
5. 这是主观判断，措辞要可证伪，不要写空泛褒扬。只输出 JSON。"""


def assess(
    backend: ModelBackend,
    *,
    title: str,
    summary: Summary,
    extracts: dict[str, ModuleExtract],
    candidates: list[dict[str, Any]] | None = None,
    projects: list[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    points = "\n".join(
        f"- [{ex.kind}] {p['text']}"
        for ex in extracts.values()
        for p in ex.points[:5]
    ) or "（无）"
    candidate_text = "\n".join(
        f"- {c.get('title', '')}（{c.get('year', '')}）" for c in (candidates or [])[:20]
    ) or "（无）"
    project_text = "、".join(projects or []) or "（未配置）"

    user = (
        f"论文标题：{title}\n"
        f"一句话：{summary.one_line}\n"
        f"五句概览：{summary.tldr}\n"
        f"局限：{'；'.join(summary.limitations)}\n\n"
        f"分节要点：\n{points}\n\n"
        f"候选相关论文（标题与年份）：\n{candidate_text}\n\n"
        f"读者当前项目：{project_text}\n\n"
        "请给出相关性、价值评估与注意事项。"
    )
    try:
        reply = call_json(backend, schema=ASSESS_SCHEMA, system=SYSTEM, user=user,
                          max_tokens=3000, task="assess")
    except ModelError as exc:
        # Optional stage: the paper is still readable without it. But the spend
        # happened, so hand the accounting back for the run log.
        meta: dict[str, Any] = {"error": f"{exc.code}: {exc}"[:200]}
        partial = getattr(exc, "reply", None)
        if partial is not None:
            meta.update(partial.accounting())
        return None, meta
    data = normalize_assessment(reply.data)
    if data is None:
        meta = reply.accounting()
        meta["error"] = "model_schema_invalid: assessment has neither value nor relevance"
        return None, meta
    data["origin"] = "llm"
    return data, reply.accounting()


_VALUE_KEYS = ("novelty", "evidence_strength", "reproducibility", "read_priority", "why")
_RELEVANCE_KEYS = ("to_library", "neighbors", "to_projects")


def normalize_assessment(data: Any) -> dict[str, Any] | None:
    """Coerce the model's reply onto the assessment shape.

    Besides the canonical nested object, models answer with the ``value``
    fields flattened to the top level, ``caveats`` as one string, and
    ``read_priority`` in Chinese or mixed case. Strict validation rejected two
    out of two real replies for exactly these; the content was fine.
    """
    if not isinstance(data, dict):
        return None
    value = data.get("value") if isinstance(data.get("value"), dict) else {}
    for k in _VALUE_KEYS:
        if k not in value and k in data:
            value[k] = data[k]
    relevance = data.get("relevance") if isinstance(data.get("relevance"), dict) else {}
    for k in _RELEVANCE_KEYS:
        if k not in relevance and k in data:
            relevance[k] = data[k]
    if not value and not relevance:
        return None
    caveats = data.get("caveats", [])
    if isinstance(caveats, str):
        caveats = [c.strip() for c in re.split(r"[;；\n]+", caveats) if c.strip()]
    elif not isinstance(caveats, list):
        caveats = []
    priority = str(value.get("read_priority", "")).strip().lower()
    priority = {"高": "high", "中": "medium", "低": "low"}.get(priority, priority)
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    value = {
        "novelty": str(value.get("novelty", "")).strip(),
        "evidence_strength": str(value.get("evidence_strength", "")).strip(),
        "reproducibility": str(value.get("reproducibility", "")).strip(),
        "read_priority": priority,
        "why": str(value.get("why", "")).strip(),
    }
    neighbors = relevance.get("neighbors", [])
    if not isinstance(neighbors, list):
        neighbors = [str(neighbors)] if neighbors else []
    projects = relevance.get("to_projects", [])
    if not isinstance(projects, list):
        projects = []
    try:
        to_library = float(relevance.get("to_library", 0) or 0)
    except (TypeError, ValueError):
        to_library = 0.0
    return {
        "relevance": {"to_library": to_library, "neighbors": [str(n) for n in neighbors],
                      "to_projects": [p for p in projects if isinstance(p, dict)]},
        "value": value,
        "caveats": [str(c) for c in caveats],
    }
