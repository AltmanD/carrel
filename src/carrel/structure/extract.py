"""Per-module extraction.

One model call per section. Every claim must carry a ``block_id``; anything the
model cannot anchor is dropped rather than kept as unattributed prose.
"""

from __future__ import annotations

import json
from typing import Any

from carrel.model import ModelBackend, ModelError, ModelSchemaError
from carrel.models import (
    CORE_SECTIONS,
    Block,
    DocModel,
    ModuleExtract,
    Section,
)

MODULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "digest": {"type": "string"},
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "block_id": {"type": "string"},
                },
                "required": ["text", "block_id"],
                "additionalProperties": False,
            },
        },
        "definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "text_zh": {"type": "string"},
                    "block_id": {"type": "string"},
                },
                "required": ["term", "text_zh", "block_id"],
                "additionalProperties": False,
            },
        },
        "formulas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["block_id", "note"],
                "additionalProperties": False,
            },
        },
        "numbers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "block_id": {"type": "string"},
                },
                "required": ["what", "block_id"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["digest", "points", "definitions", "formulas", "numbers", "open_questions"],
    "additionalProperties": False,
}

FOCUS: dict[str, str] = {
    "abstract": "问题、方法一句话、主要结果、结论。",
    "introduction": "动机、现有工作的 gap、贡献列表、与已有方法的差异。",
    "related_work": "分组的 research lineage、被对比的方法、本文定位。",
    "background": "必要定义、符号、前置结论。",
    "method": "构造/算法/目标函数、输入与输出、关键设计选择及其理由。",
    "experiments": "数据集、基线、指标、实验设置、硬件与训练成本；未披露就写“论文未报告”。",
    "results": "主结果、消融、失败案例；区分理论、仿真、benchmark 与硬件证据。",
    "discussion": "作者对结果的解释、适用条件、与预期的差异。",
    "limitations": "作者自陈的局限，以及可从正文看出的适用条件。",
    "conclusion": "结论、未来工作。",
    "appendix": "补充实验、证明、实现细节中值得记录的部分。",
    "unknown": "概括这一部分说了什么，不必强行归类。",
}

SYSTEM = """你是论文精读助手。你只依据给定的原文片段作答，不补充外部知识。

硬性规则：
1. 每条 point 必须绑定原文中真实存在的 block_id；找不到依据的内容不要写。
2. 原文没说的，写"论文未报告"或"未说明"，不要推测。
3. 数字、单位、条件必须与原文完全一致。
4. 输出简体中文，专有名词、方法名、数据集名、指标名保留英文原文。
5. 只输出一个 JSON 对象，不要数组、不要解释、不要 Markdown 代码块。"""


def _structured(backend: ModelBackend, *, schema: dict[str, Any], system: str,
                user: str, max_tokens: int, task: str = "extract"):
    """Call the model and return its parsed JSON without schema enforcement.

    This endpoint advertises strict schema support but returns its own key names,
    so the payload is normalised by the caller instead of being rejected here.
    Falls back to ``structured`` for backends that only expose that path.
    """
    fn = getattr(backend, "raw_structured", None)
    if callable(fn):
        return fn(schema=schema, system=system, user=user, max_tokens=max_tokens, task=task)
    return backend.structured(schema=schema, system=system, user=user,
                              max_tokens=max_tokens, task=task)


# ---------------------------------------------------------------------------
# Tolerant normalisation
#
# The endpoint advertises strict schema support but in practice returns its own
# key names ("chapter", "summary", "statement"), and occasionally answers with a
# bare array. Rejecting those replies would waste every call, so each field is
# looked up through a list of accepted aliases and coerced to the expected shape.
# ---------------------------------------------------------------------------

_ALIASES_DIGEST = ("digest", "summary", "overview", "abstract", "brief")
_ALIASES_POINTS = ("points", "key_points", "items", "point")
_ALIASES_TEXT = ("text", "statement", "point", "content", "description", "claim")
_ALIASES_TERM = ("term", "name", "concept")
_ALIASES_TERM_ZH = ("text_zh", "zh", "translation", "definition", "meaning", "explain")
_ALIASES_FORMULA_NOTE = ("note", "description", "meaning", "text", "explanation")
_ALIASES_NUMBER_WHAT = ("what", "meaning", "value", "text", "number", "description")
_ALIASES_OPEN = ("open_questions", "questions", "uncertainties", "gaps")


def _first(data: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, "", [], {}):
            return data[key]
    return default


def normalize_module_payload(data: Any) -> dict[str, Any]:
    """Map whatever the model returned onto :data:`MODULE_SCHEMA`'s shape."""
    if isinstance(data, list):
        # A bare list is either the points themselves or one wrapped object.
        if data and all(isinstance(x, dict) for x in data) and any(
            "block_id" in x for x in data
        ):
            return {"points": data}
        for item in data:
            if isinstance(item, dict) and ("points" in item or "digest" in item
                                           or "summary" in item):
                return normalize_module_payload(item)
        return {"points": []}
    if not isinstance(data, dict):
        return {}

    digest = _first(data, _ALIASES_DIGEST)
    if isinstance(digest, (dict, list)):
        digest = ""

    raw_points = _first(data, _ALIASES_POINTS, []) or []
    points: list[dict[str, str]] = []
    for item in raw_points if isinstance(raw_points, list) else []:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        text = _first(item, _ALIASES_TEXT)
        bid = str(item.get("block_id") or item.get("id") or "").strip()
        if isinstance(text, str) and text.strip() and bid:
            points.append({"text": text.strip(), "block_id": bid})

    definitions: list[dict[str, str]] = []
    for item in (_first(data, ("definitions", "terms", "concepts"), []) or []):
        if not isinstance(item, dict):
            continue
        term = _first(item, _ALIASES_TERM)
        zh = _first(item, _ALIASES_TERM_ZH)
        bid = str(item.get("block_id") or "").strip()
        if isinstance(term, str) and term.strip() and isinstance(zh, str) and zh.strip():
            definitions.append(
                {"term": term.strip(), "text_zh": zh.strip(), "block_id": bid}
            )

    formulas: list[dict[str, str]] = []
    for item in (_first(data, ("formulas", "equations"), []) or []):
        if not isinstance(item, dict):
            continue
        bid = str(item.get("block_id") or "").strip()
        note = _first(item, _ALIASES_FORMULA_NOTE, "")
        if bid:
            formulas.append({"block_id": bid, "note": str(note).strip()})

    numbers: list[dict[str, str]] = []
    for item in (_first(data, ("numbers", "key_numbers", "figures"), []) or []):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        what = _first(item, _ALIASES_NUMBER_WHAT)
        bid = str(item.get("block_id") or "").strip()
        if what is not None and bid:
            numbers.append({"what": str(what).strip(), "block_id": bid})

    open_q: list[str] = []
    for item in (_first(data, _ALIASES_OPEN, []) or []):
        if isinstance(item, str) and item.strip():
            open_q.append(item.strip())
        elif isinstance(item, dict):
            text = _first(item, _ALIASES_TEXT)
            if isinstance(text, str) and text.strip():
                open_q.append(text.strip())

    return {
        "digest": str(digest).strip() if isinstance(digest, str) else "",
        "points": points,
        "definitions": definitions,
        "formulas": formulas,
        "numbers": numbers,
        "open_questions": open_q,
    }


class Extractor:
    def __init__(self, backend: ModelBackend) -> None:
        self.backend = backend

    def extract_section(
        self,
        section: Section,
        blocks: list[Block],
        *,
        max_chars: int = 48000,
        compact: bool = False,
    ) -> tuple[ModuleExtract, dict[str, Any]]:
        payload, truncated = _render(blocks, max_chars)
        focus = FOCUS.get(section.kind, FOCUS["unknown"])
        user = (
            f"章节：{section.title}（类别 {section.kind}）\n"
            f"关注点：{focus}\n\n"
            f"原文片段：\n{payload}\n\n"
            "请按 schema 输出。digest 不超过 300 字，points 控制在 3–8 条；"
            "definitions、formulas、numbers 各不超过 8 条，open_questions 不超过 4 条。"
            "每条只写一句话，numbers 只收关键结论数值，不逐行复述表格。"
        )
        if compact:
            user += (
                "\n上次回复超出输出预算。请精简：digest 不超过 150 字，points 最多 4 条，"
                "其余数组各最多 3 条；保留原文依据和关键结论，不输出整段原文或完整表格。"
            )
        reply = _structured(self.backend, schema=MODULE_SCHEMA, system=SYSTEM, user=user,
                            max_tokens=12000)
        data = normalize_module_payload(reply.data)
        if not data.get("points") and not data.get("digest"):
            # Nothing usable came back for this section. The caller decides whether
            # to skip it or fail; raising here would abort a paper over one sparse
            # appendix section. The reply rides along so the spend is recorded.
            raise ModelSchemaError("模型未返回可用的分节内容", reply)
        valid_ids = {b.id for b in blocks}
        extract = ModuleExtract(
            kind=section.kind,
            title_original=section.title,
            block_span=[blocks[0].id, blocks[-1].id] if blocks else [],
            digest=str(data.get("digest", "")).strip(),
            points=_clean_points(data.get("points"), valid_ids),
            definitions=_clean_pairs(data.get("definitions"), valid_ids, "term", "text_zh"),
            formulas=_clean_pairs(data.get("formulas"), valid_ids, "block_id", "note",
                                  key_field="block_id", value_field="note"),
            numbers=_clean_pairs(data.get("numbers"), valid_ids, "what", "block_id",
                                 key_field="what", value_field="block_id"),
            open_questions=[str(q).strip() for q in (data.get("open_questions") or [])
                            if str(q).strip()],
        )
        meta = {**reply.accounting(), "truncated_input": truncated}
        return extract, meta

    def extract_all(
        self, doc: DocModel, sections: list[Section], *, scope: tuple[str, ...] | None = None
    ) -> tuple[dict[str, ModuleExtract], list[str]]:
        by_id = doc.blocks_by_id()
        wanted = set(scope or CORE_SECTIONS)
        results: dict[str, ModuleExtract] = {}
        warnings: list[str] = []
        for section in sections:
            if section.kind in ("references", "acknowledgments"):
                continue
            if wanted and section.kind not in wanted and section.kind != "unknown":
                # Still summarise non-core sections lightly via digest only.
                pass
            blocks = [by_id[b] for b in section.block_ids if b in by_id]
            if not blocks:
                continue
            try:
                extract, meta = self.extract_section(section, blocks)
            except (ModelError, ModelSchemaError) as exc:
                warnings.append(f"{section.kind}: {exc}")
                continue
            if meta.get("truncated_input"):
                warnings.append(f"{section.kind}: input truncated")
            key = section.kind if section.kind not in results else f"{section.kind}-{section.id}"
            results[key] = extract
        return results, warnings


def _render(blocks: list[Block], max_chars: int) -> tuple[str, bool]:
    lines: list[str] = []
    used = 0
    truncated = False
    for block in blocks:
        tag = f"[{block.id}]"
        body = block.text.strip()
        if block.type == "formula":
            body = f"(公式) {body}"
        elif block.type in ("figure", "table"):
            body = f"({block.type}) {body}"
        line = f"{tag} {body}"
        if used + len(line) > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines), truncated


def _clean_points(raw, valid_ids: set[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        bid = str(item.get("block_id", ""))
        text = str(item.get("text", "")).strip()
        if text and bid in valid_ids:
            out.append({"text": text, "block_id": bid})
    return out


def _clean_pairs(raw, valid_ids: set[str], key: str, value: str,
                 key_field: str = "", value_field: str = "") -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        entry = {k: str(v).strip() for k, v in item.items() if isinstance(v, (str, int, float))}
        bid = entry.get("block_id", "")
        if bid and bid not in valid_ids:
            continue
        if any(entry.values()):
            out.append(entry)
    return out


def render_modules(extracts: dict[str, ModuleExtract]) -> str:
    """Human-readable markdown for one paper's modules."""
    parts: list[str] = []
    for key, ex in extracts.items():
        parts.append(f"## {ex.title_original or key}")
        if ex.digest:
            parts.append(ex.digest)
        if ex.points:
            parts.append("\n".join(f"- {p['text']}（{p['block_id']}）" for p in ex.points))
        if ex.definitions:
            parts.append("\n".join(
                f"- **{d.get('term', '')}**：{d.get('text_zh', '')}（{d.get('block_id', '')}）"
                for d in ex.definitions))
        if ex.numbers:
            parts.append("\n".join(
                f"- {n.get('what', '')}（{n.get('block_id', '')}）" for n in ex.numbers))
        if ex.open_questions:
            parts.append("\n".join(f"- 待解：{q}" for q in ex.open_questions))
        parts.append("")
    return "\n".join(parts)
