"""Compact, evidence-based graph names from existing titles and terminology."""
from __future__ import annotations

import re


def _normalized(text: str) -> str:
    return re.sub(r"[-‐‑–—]", " ", text).casefold()


def paper_label(title: str, title_zh: str = "", glossary: dict | None = None,
                summary: dict | None = None) -> str:
    """Prefer a named contribution; otherwise use a short research topic.

    This is a read-only display projection, not a new model inference. A
    glossary note or contribution must explicitly attribute a method to this
    paper before a name absent from the title can be selected.
    """
    title = " ".join(str(title or "").split()).replace("$", "")
    parts = re.split(r"[:：]", title, maxsplit=1)
    named = parts[0].strip() if len(parts) > 1 else ""
    if named and (len(named) > 42 or len(named.split()) > 5 or
                  re.search(r"^(towards?|when|what|where|why|how|don't|think|turns|beneath)\b", named, re.I)):
        named = ""
    # Distinctive named title prefixes are stronger evidence than an algorithm
    # mentioned as a component in the terminology notes (e.g. EmoReward / GRPO).
    if named and (len(named.split()) == 1 or re.search(r"[a-z][A-Z]|[A-Z]{2,}", named)):
        return named
    if len(parts) > 1 and re.fullmatch(r"[A-Za-z][A-Za-z0-9^.-]{1,25}", parts[-1].strip()):
        return parts[-1].strip()
    summary = summary if isinstance(summary, dict) else {}
    contributions = summary.get("contributions") or []
    evidence = "。".join(str(x) for x in [summary.get("one_line", ""),
                         *(contributions if isinstance(contributions, list) else [])])
    candidates, topics = [], []
    terms = (glossary or {}).get("terms", [])
    for index, term in enumerate(terms if isinstance(terms, list) else []):
        if not isinstance(term, dict):
            continue
        source = str(term.get("src") or term.get("term") or term.get("en") or "").strip()
        note = str(term.get("note") or "")
        if not source or len(source) > 80:
            continue
        base = re.split(r"\s[/／]\s|[（(]", source)[0].strip()
        zh = str(term.get("zh") or "").strip()
        label = base if term.get("keep_original") or not zh or zh == "原文" else zh
        own = re.search(r"(?:本文|论文|本研究)(?:中|的)?(?:提出|设计|构建|引入|核心方法|核心框架|方法名|模型名|系统名|框架名)", note)
        introduced = re.search(r"(?:提出|设计|构建|组合为|命名为)\s*(?:了|一种|一个|名为)*\s*" + re.escape(base) + r"(?![A-Za-z])", evidence, re.I)
        excluded = re.search(r"^(?:基线|子模块|变体|子机制|指标|数据集)|(?:基线|指标|数据集)名", note)
        if (own or introduced) and not excluded and len(label) <= 42:
            candidates.append((0 if base.casefold() == named.casefold() else 1, 0 if own else 1, index, label))
        if len(base) >= 5 and _normalized(base) in _normalized(title) and len(label) <= 42:
            topics.append((len(base.split()) >= 2, len(base), -index, label))
    if candidates:
        return min(candidates)[3]
    if named:
        return named
    if topics:
        return max(topics)[3]
    # Rhetorical study titles have no named method; retain their subject.
    for pattern in (r"\babout (.+?)[?.]?$", r"^are (.+?) worth\b",
                    r"^what does (.+?) (?:really )?improve\b"):
        match = re.search(pattern, title, re.I)
        if match:
            return match[1].strip()
    fallback = (title_zh or title or "未命名论文").strip()
    # Unnamed studies use a topic fragment; keep the full title in details.
    segments = re.split(r"[:：?？]", fallback)
    if len(segments) > 1:
        fallback = next((part.strip() for part in reversed(segments) if part.strip()), fallback)
    if re.search(r"[\u3400-\u9fff]", fallback):
        # Avoid cutting a Latin word in half in mixed Chinese/English titles.
        if len(fallback) > 24:
            fallback = re.sub(r"[A-Za-z]+$", "", fallback[:24])
        return fallback.rstrip("，、：； ）")
    return " ".join(fallback.split()[:6])
