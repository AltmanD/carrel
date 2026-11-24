"""Glossary construction and locking.

The glossary is built before translation and then held fixed, because the most
common translation failure is one concept drifting into three renderings across
a long paper. Editing the glossary only re-translates the affected modules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from carrel.model import ModelBackend, ModelError
from carrel.model.base import as_list, call_json, pick
from carrel.models import ModuleExtract
from carrel.store.paths import atomic_write_json, read_json

GLOSSARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "zh": {"type": "string"},
                    "note": {"type": "string"},
                    "keep_original": {"type": "boolean"},
                },
                "required": ["src", "zh", "keep_original"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["terms"],
    "additionalProperties": False,
}

SYSTEM = """你在为论文翻译建立术语表。规则：
1. 只收真正影响理解的技术术语、方法名、指标名、缩写。
2. 模型名、数据集名、指标名、框架名一律 keep_original=true（不翻译）。
3. 普通学术词汇（method、result、experiment）不要收。
4. 每个术语给一个固定的中文译法，后续全文必须一致。
5. 只输出 JSON。"""

# Terms a paper rarely wants translated even when it defines them.
KEEP_ORIGINAL_HINT = ("gpt", "llm", "bert", "llama", "qwen", "clip", "imagenet",
                      "coco", "squad", "glue", "mmlu", "humaneval", "swe-bench")


class Glossary:
    def __init__(self, terms: list[dict[str, Any]] | None = None, locked: bool = False):
        self.terms = terms or []
        self.locked = locked

    # ------------------------------------------------------------ io
    @classmethod
    def load(cls, path: Path) -> "Glossary":
        """Load a saved table; a missing, corrupt or mis-shaped file is an empty one.

        This is read on every translate stage, including retries: a half-written
        file (power loss mid-save) that raised here parked the paper in
        ``translating`` for good, because the exception escaped every handler.
        """
        data = read_json(path, None)
        if not isinstance(data, dict):
            return cls()
        terms = data.get("terms")
        return cls(terms=[t for t in terms if isinstance(t, dict)] if isinstance(terms, list) else [],
                   locked=bool(data.get("locked")))

    def save(self, path: Path) -> None:
        atomic_write_json(path, {"terms": self.terms, "locked": self.locked})

    # --------------------------------------------------------- queries
    def as_prompt_table(self) -> str:
        if not self.terms:
            return "（无）"
        rows = []
        for t in self.terms:
            src = t.get("src", "")
            zh = t.get("zh", "")
            if t.get("keep_original"):
                rows.append(f"- {src} → 保留原文不译")
            else:
                note = f"（{t['note']}）" if t.get("note") else ""
                rows.append(f"- {src} → {zh}{note}")
        return "\n".join(rows)

    def by_src(self) -> dict[str, dict[str, Any]]:
        return {str(t.get("src", "")).lower(): t for t in self.terms if t.get("src")}

    def as_terms_map(self) -> dict[str, str]:
        """Lower-cased source term -> agreed Chinese rendering.

        Terms flagged ``keep_original`` map to ``"原文"`` and are skipped by the
        glossary consistency check, since they are expected to stay English.
        """
        out: dict[str, str] = {}
        for t in self.terms:
            src = str(t.get("src", "")).strip().lower()
            if not src:
                continue
            if t.get("keep_original"):
                out[src] = "原文"
            else:
                out[src] = str(t.get("zh", "")).strip()
        return out

    def add(self, src: str, zh: str, *, keep_original: bool = False, note: str = "") -> None:
        if not src or any(t.get("src", "").lower() == src.lower() for t in self.terms):
            return
        self.terms.append(
            {"src": src, "zh": zh, "note": note, "keep_original": keep_original}
        )

    def subset_for(self, text: str) -> "Glossary":
        """Only the terms that actually occur in ``text`` (whole-word, case-insensitive).

        A repair prompt for two paragraphs does not need the paper's whole term
        table; sending just the hits keeps the prompt small and, more
        importantly, keeps the consistency check and the prompt in agreement
        about which terms are in play.
        """
        low = (text or "").lower()
        hits = [t for t in self.terms if _term_present(low, str(t.get("src", "")).lower())]
        return Glossary(terms=hits, locked=self.locked)


def _term_present(low_text: str, term: str) -> bool:
    """Whole-word presence, mirroring the QA check so both agree."""
    if not term:
        return False
    start = 0
    while True:
        idx = low_text.find(term, start)
        if idx == -1:
            return False
        before = low_text[idx - 1] if idx > 0 else ""
        after = low_text[idx + len(term)] if idx + len(term) < len(low_text) else ""
        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
            return True
        start = idx + 1


def _normalize_terms(data: Any) -> list[dict[str, Any]]:
    """Accept the envelope shapes the gateway actually returns for a term table."""
    items = pick(data, "terms", "glossary", "entries", "items", default=None)
    if items is None and isinstance(data, list):
        items = data
    out: list[dict[str, Any]] = []
    for item in as_list(items) if not isinstance(items, list) else items:
        if isinstance(item, str) and "→" in item:
            src, _, zh = item.partition("→")
            item = {"src": src.strip(), "zh": zh.strip()}
        if not isinstance(item, dict):
            continue
        src = str(pick(item, "src", "term", "source", "en", default="")).strip()
        if not src:
            continue
        out.append({
            "src": src,
            "zh": str(pick(item, "zh", "target", "translation", "cn", default="")).strip(),
            "note": str(pick(item, "note", "comment", default="")).strip(),
            "keep_original": bool(pick(item, "keep_original", "keep", default=False)),
        })
    return out


def build_glossary(
    backend: ModelBackend,
    *,
    title: str,
    abstract: str,
    extracts: dict[str, ModuleExtract],
) -> tuple[Glossary, dict[str, Any]]:
    """Ask the model for the paper's term table, seeded from extracted definitions.

    Returns ``(glossary, run_meta)``. The glossary is best-effort — translation
    still runs with an empty table — but the failure is reported in ``run_meta``
    rather than swallowed: four of the first five real calls were truncated and
    silently produced an empty table, which then overwrote a good one on disk.
    """
    seed_terms: list[str] = []
    for ex in extracts.values():
        for d in ex.definitions:
            term = str(d.get("term", "")).strip()
            if term and term not in seed_terms:
                seed_terms.append(term)

    definitions_text = "\n".join(
        f"- {d.get('term', '')}：{d.get('text_zh', '')}"
        for ex in extracts.values()
        for d in ex.definitions
    ) or "（无）"

    user = (
        f"论文标题：{title}\n"
        f"摘要：{abstract[:2000] or '（无）'}\n\n"
        f"抽取阶段已识别的定义：\n{definitions_text}\n\n"
        "请给出本文的翻译术语表（10–40 条）。"
    )
    glossary = Glossary()
    meta: dict[str, Any] = {}
    try:
        reply = call_json(backend, schema=GLOSSARY_SCHEMA, system=SYSTEM, user=user,
                          max_tokens=8000, task="glossary")
        meta = reply.accounting()
        for item in _normalize_terms(reply.data):
            src = item["src"]
            keep = item["keep_original"]
            if any(hint in src.lower() for hint in KEEP_ORIGINAL_HINT):
                keep = True
            glossary.add(
                src,
                "原文" if keep else item["zh"] or src,
                keep_original=keep,
                note=item["note"],
            )
    except ModelError as exc:
        if getattr(exc, "reply", None) is not None:
            meta = exc.reply.accounting()
        meta["error"] = f"{exc.code}: {exc}"[:200]

    # Definitions carry a Chinese *explanation*, not a rendering. Seeding a whole
    # sentence as the mandatory translation would make every block containing the
    # term permanently fail the glossary check, so only short, noun-like renderings
    # are reused here.
    for ex in extracts.values():
        for d in ex.definitions:
            term = str(d.get("term", "")).strip()
            zh = str(d.get("text_zh", "")).strip()
            if not term or not zh:
                continue
            if any(h in term.lower() for h in KEEP_ORIGINAL_HINT):
                continue
            if len(zh) > 8 or zh.endswith(("。", "，", "；", ":", "：")):
                continue  # a sentence, not a term rendering
            glossary.add(term, zh)
    return glossary, meta
