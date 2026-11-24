"""Classification and evidence-backed tags."""

from __future__ import annotations

import re
from typing import Any

from carrel.classify.taxonomy import UNCLASSIFIED, Taxonomy, category_id, project_path
from carrel.model import ModelBackend
from carrel.model.base import as_list, call_json, pick
from carrel.models import Classification, ModuleExtract, Summary, Tag

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "description": "1–2 most specific matching paths from the taxonomy",
            "items": {
                "type": "object",
                "properties": {
                    "discipline": {"type": "string"},
                    "field": {"type": "string"},
                },
                "required": ["discipline", "field"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number"},
        "basis": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "evidence": {"type": "string"},
                    "block_id": {"type": "string"},
                },
                "required": ["tag", "evidence", "block_id"],
                "additionalProperties": False,
            },
        },
        "proposal": {
            "type": "object",
            "properties": {
                "discipline": {"type": "string"},
                "field": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["discipline", "field", "reason"],
            "additionalProperties": False,
        },
    },
    "required": ["categories", "confidence", "basis", "reason", "tags"],
    "additionalProperties": False,
}

SYSTEM = """你在为论文分类与打标签。规则：
1. categories 给出 1–2 条**最具体**的分类路径（仅一级方向和二级主题，不生成 subfield），必须从给定分类表中选择；一篇论文可以同时属于多个子领域。每条路径都必须是论文的实际研究内容，有分节摘要可以支撑——不要为了凑数多给，也不要给"顺带提到"的方向。宁可停在能支撑的一级，不要选一个更具体但不准确的子领域。
2. 如果分类表确实没有合适的子领域，categories 返回空数组，并在 proposal 里说明建议新增什么、为什么。不要用一个宽泛的大类来兜底。
3. 标签规则：
   - 每条是领域内**真实通行的叫法**，不是描述。判据：中文社区如果习惯用英文（process reward、dense reward、prompt selection、winner-label），就保留英文原文；中文叫法确实通行才用中文（强化学习、稠密检索）。
   - 严禁生造直译：winner-label 不译成"赢家标签"，quality floor 不译成"质量地板"。拿不准就保留英文。
   - 中文标签 2–8 字；英文标签用原文词组（含空格）。
   - 给 3–5 条，每条附 evidence（原文真实片段，≤40 字）与 block_id。
   - 与分类路径重复的词不要再用作标签。
4. basis 列出支撑判断的 block_id。
5. 只输出 JSON。"""

#: A usable tag is short and looks like a term, not a sentence. English
#: term groups ("process reward") run longer than Chinese terms, so they get
#: their own cap.
MAX_TAG_CHARS = 12
MAX_EN_TAG_CHARS = 24


class Classifier:
    def __init__(self, backend: ModelBackend) -> None:
        self.backend = backend

    def classify(
        self,
        *,
        taxonomy: Taxonomy,
        title: str,
        summary: Summary,
        extracts: dict[str, ModuleExtract],
    ) -> tuple[Classification, dict[str, Any], dict[str, Any] | None]:
        digests = "；".join(
            f"{ex.kind}: {ex.digest}" for ex in extracts.values() if ex.digest
        )
        user = (
            f"可选分类路径（两级）：\n{taxonomy.prompt_table()}\n\n"
            f"论文标题：{title}\n"
            f"一句话：{summary.one_line}\n"
            f"分节摘要：{digests[:3000]}\n\n"
            "请给出最具体的分类路径（1–2 条）、置信度、依据与 3–5 个短标签。"
        )
        reply = call_json(self.backend, schema=CLASSIFY_SCHEMA, system=SYSTEM,
                          user=user, max_tokens=4000, task="classify")
        data = reply.data if isinstance(reply.data, dict) else {}

        raw_categories = as_list(pick(data, "categories", "category", "paths", default=[]))
        categories: list[dict[str, str]] = []
        proposal: dict[str, Any] | None = None
        for item in raw_categories:
            # Shapes seen: "计算机科学 / 机器学习 / 稠密检索" shorthand; {"path": "A / B / C",
            # "confidence": 0.9}; {discipline, field, subfield}. The slash form
            # is what a model naturally writes back from the slash-separated
            # taxonomy table it was shown, so it must not fall through to 未分类.
            if isinstance(item, dict) and isinstance(item.get("path"), str) and "discipline" not in item:
                item = item["path"]
            if isinstance(item, str):
                parts = [p.strip() for p in item.split("/")]
                item = {"discipline": parts[0] if parts else "",
                        "field": parts[1] if len(parts) > 1 else "",
                        "subfield": parts[2] if len(parts) > 2 else ""}
            if not isinstance(item, dict):
                continue
            projected = project_path(item, taxonomy.renames)
            discipline = str(projected.get("discipline", "")).strip()
            field_name = str(projected.get("field", "")).strip()
            subfield = None
            if not discipline:
                continue
            ok, _why = taxonomy.validate(discipline, field_name, subfield)
            if ok:
                candidate = {"discipline": discipline, "field": field_name,
                             "category_id": taxonomy.id_for(discipline, field_name)}
                if candidate in categories:
                    continue
                categories.append(candidate)
                if len(categories) >= 2:
                    break
        if not categories:
            proposal = pick(data, "proposal", default=None)
            if isinstance(proposal, dict) and proposal.get("discipline"):
                pass  # keep as the suggestion for a human
            else:
                proposal = None
            categories = [project_path({"discipline": UNCLASSIFIED, "field": ""})]

        raw_tags = as_list(pick(data, "tags", "labels", default=[]))
        tags: list[Tag] = []
        for t in raw_tags:
            if isinstance(t, str):
                t = {"tag": t, "evidence": "", "block_id": ""}
            if not isinstance(t, dict):
                continue
            label = str(t.get("tag", "")).strip()
            bid = str(t.get("block_id", "")).strip()
            # Enforce the term-like constraint in code too: the model still
            # produces sentence tags under pressure.
            if not label or not bid:
                continue
            ascii_like = sum(1 for ch in label if ch.isascii()) >= len(label) * 0.7
            if len(label) > (MAX_EN_TAG_CHARS if ascii_like else MAX_TAG_CHARS):
                continue
            tags.append(Tag(
                tag=label,
                evidence=str(t.get("evidence", "")).strip()[:80],
                block_id=bid,
                origin="llm",
            ))

        # Second-pass curation: the generator over-produces metric names and
        # incidentals ("pass@1") that carry no browsing value. This used to be a
        # second model call with a 500-token ceiling, which a thinking model
        # truncated almost every time — so it silently did nothing and cost
        # money. The rules it was given are mechanical; apply them locally.
        category_terms = {p[k].casefold() for p in categories for k in ('discipline', 'field') if p[k]}
        tags = [tag for tag in curate_tags(tags) if tag.tag.casefold() not in category_terms][:5]

        primary = categories[0]
        result = Classification(
            discipline=primary["discipline"],
            field_name=primary["field"],
            primary_category_id=primary.get("category_id") or taxonomy.id_for(primary["discipline"], primary["field"]),
            confidence=float(pick(data, "confidence", "score", default=0) or 0),
            basis=[str(b) for b in as_list(pick(data, "basis", default=[]))
                   if str(b).strip()],
            reason=str(pick(data, "reason", default="")).strip(),
            tags=tags,
            categories=categories,  # type: ignore[arg-type]
        )
        return result, reply.accounting(), proposal


# ------------------------------------------------------------------ curation
#: Metric names and benchmark-score tokens: never a browsing topic.
_METRIC_TAG = re.compile(
    r"^(?:pass@\d+|acc(?:uracy)?|f1(?:[- ]score)?|bleu|rouge(?:-[l\d])?|auc|"
    r"mrr|ndcg(?:@\d+)?|em|exact match|win[- ]rate|perplexity|ppl|map|recall|"
    r"precision|latency|throughput|loss|error rate|wer|cer)$",
    re.I,
)

#: Words every paper in the field would carry; they filter nothing.
_GENERIC_TAGS = {
    "deep learning", "machine learning", "neural network", "neural networks",
    "large language model", "large language models", "llm", "llms", "ai",
    "artificial intelligence", "evaluation", "benchmark", "experiment",
    "experiments", "method", "methods", "model", "models", "dataset", "datasets",
    "training", "inference", "performance", "framework", "approach", "algorithm",
    "深度学习", "机器学习", "神经网络", "大语言模型", "大模型", "人工智能", "评估",
    "基准", "实验", "方法", "模型", "数据集", "训练", "推理", "性能", "框架", "算法",
}


def curate_tags(tags: list[Tag]) -> list[Tag]:
    """Normalize aliases; remove metrics, generic words and duplicates."""
    kept: list[Tag] = []
    seen = set()
    aliases = {'kv cache': 'KV Cache', 'kv-cache': 'KV Cache', 'grpo': 'GRPO',
               'ppo': 'PPO', 'swe bench': 'SWE-bench', 'swe-bench': 'SWE-bench',
               'reward-hacking': 'reward hacking', 'self play': 'self-play'}
    for tag in tags:
        label = ' '.join(tag.tag.split())
        label = aliases.get(label.casefold(), label)
        key = label.casefold()
        if key in seen:
            continue
        if _METRIC_TAG.match(key):
            continue
        if key in _GENERIC_TAGS or key.rstrip("s") in _GENERIC_TAGS:
            continue
        tag.tag = label
        seen.add(key)
        kept.append(tag)
    return kept
