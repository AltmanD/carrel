"""Whole-paper term review.

After every module is translated, one extra call sees the glossary plus where
each term actually appeared, and proposes replacements. The model only proposes;
the program applies them and writes a diff, so this stage cannot silently
rewrite prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from carrel.model import ModelBackend, ModelError
from carrel.model.base import call_json, pick
from carrel.translate.glossary import Glossary

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "replacements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "block_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["from", "to", "block_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["replacements"],
    "additionalProperties": False,
}

SYSTEM = """你在做全篇术语一致性复核。规则：
1. 只允许替换术语表里已经出现的词及其变体，不得改写其他内容。
2. 每次替换给出 from（当前写法）、to（应统一为的写法）、涉及的 block_id。
3. 如果某词已经一致，不要输出。
4. 只输出 JSON；没有需要改的就返回空数组。"""


@dataclass(slots=True)
class Replacement:
    source: str
    target: str
    block_ids: list[str]
    reason: str = ""


@dataclass(slots=True)
class TermReviewResult:
    replacements: list[dict[str, Any]] = field(default_factory=list)
    applied: int = 0
    diff: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    # Accounting for the one model call behind this result (empty when the
    # stage was skipped because there was nothing to review).
    run_meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replacements": self.replacements,
            "applied": self.applied,
            "diff": self.diff,
            "rejected": self.rejected,
        }


class TermReviewer:
    def __init__(self, backend: ModelBackend) -> None:
        self.backend = backend

    def review(
        self,
        *,
        glossary: Glossary,
        translations_by_kind: dict[str, dict[str, str]],
    ) -> TermReviewResult:
        if not glossary.terms or not translations_by_kind:
            return TermReviewResult()

        occurrences = _occurrences(glossary, translations_by_kind)
        if not occurrences:
            return TermReviewResult()

        user = (
            f"术语表：\n{glossary.as_prompt_table()}\n\n"
            f"各术语在译文中的实际出现情况：\n{occurrences}\n\n"
            "请给出需要统一替换的条目。"
        )
        try:
            reply = call_json(self.backend, schema=REVIEW_SCHEMA, system=SYSTEM, user=user,
                              max_tokens=4000, task="term_review")
        except ModelError as exc:
            # The translation on disk is complete; a failed review just means no
            # cross-module unification this time. Keep the spend visible.
            result = TermReviewResult()
            result.run_meta = {"error": f"{exc.code}: {exc}"[:200]}
            partial = getattr(exc, "reply", None)
            if partial is not None:
                result.run_meta.update(partial.accounting())
            return result
        proposed = _normalize_replacements(reply.data)
        result = TermReviewResult(replacements=proposed, run_meta=reply.accounting())
        allowed = {t.get("src", "").lower() for t in glossary.terms if t.get("src")}
        allowed_zh = {t.get("zh", "") for t in glossary.terms if t.get("zh")}

        for item in proposed:
            src = str(item.get("from", "")).strip()
            dst = str(item.get("to", "")).strip()
            bids = [str(b) for b in item.get("block_ids", [])]
            # Only replacements that touch a known glossary term are allowed.
            if not src or not dst or not bids:
                result.rejected.append({**item, "why": "incomplete"})
                continue
            if src.lower() not in allowed and src not in allowed_zh:
                result.rejected.append({**item, "why": "not a glossary term"})
                continue
            for kind, table in translations_by_kind.items():
                for bid in bids:
                    if bid not in table:
                        continue
                    before = table[bid]
                    if src not in before:
                        continue
                    after = before.replace(src, dst)
                    table[bid] = after
                    result.applied += 1
                    result.diff.append({
                        "kind": kind, "block_id": bid,
                        "from": src, "to": dst,
                    })
        return result


def _normalize_replacements(data: Any) -> list[dict[str, Any]]:
    """Accept the envelopes models return for a replacement list.

    Canonical is ``{"replacements": [{from, to, block_ids}]}``; glm-5.3 returns
    a bare list, and names the id field ``block_id`` (sometimes a string,
    sometimes a list). The program still only applies replacements that name a
    glossary term, so being lenient here cannot rewrite prose.
    """
    items = pick(data, "replacements", "items", "changes", default=None)
    if items is None and isinstance(data, list):
        items = data
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        bids = item.get("block_ids", item.get("block_id", item.get("blocks", [])))
        if isinstance(bids, str):
            bids = [bids]
        out.append({
            "from": str(pick(item, "from", "source", "current", default="")).strip(),
            "to": str(pick(item, "to", "target", "replacement", default="")).strip(),
            "block_ids": [str(b) for b in bids] if isinstance(bids, list) else [],
            "reason": str(item.get("reason", "")).strip(),
        })
    return out


def _occurrences(glossary: Glossary, tables: dict[str, dict[str, str]]) -> str:
    lines: list[str] = []
    joined = {k: "\n".join(v.values()) for k, v in tables.items()}
    for term in glossary.terms:
        src = term.get("src", "")
        zh = term.get("zh", "")
        variants = {src}
        if zh and zh != "原文":
            variants.add(zh)
        hits = []
        for kind, text in joined.items():
            for variant in variants:
                if variant and variant in text:
                    count = text.count(variant)
                    hits.append(f"{kind}:{variant}×{count}")
                    break
        if hits:
            lines.append(f"- {src}（约定译法「{zh}」）出现于 " + ", ".join(hits))
    return "\n".join(lines) or "（无）"
