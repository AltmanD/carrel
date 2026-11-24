"""Layered summary.

Built only after per-module extraction exists, and ``section_digests`` is copied
from the module digests rather than regenerated — otherwise the two layers drift
apart and the summary starts asserting things the modules never said.
"""

from __future__ import annotations

import re
from typing import Any

from carrel.model import ModelBackend, ModelError, ModelSchemaError, ModelTruncated
from carrel.model.base import as_list, call_json, pick
from carrel.models import ModuleExtract, Summary

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "one_line": {"type": "string"},
        "tldr": {"type": "string"},
        "contributions": {"type": "array", "items": {"type": "string"}},
        "argument_chain": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "reading_guide": {
            "type": "object",
            "properties": {
                "order": {"type": "array", "items": {"type": "string"}},
                "skip": {"type": "array", "items": {"type": "string"}},
                "why": {"type": "string"},
            },
            "required": ["order", "skip", "why"],
            "additionalProperties": False,
        },
    },
    "required": ["one_line", "tldr", "contributions", "argument_chain",
                 "limitations", "reading_guide"],
    "additionalProperties": False,
}

SYSTEM = """你在为一篇已逐节精读过的论文写导读。规则：
1. one_line 一句话说清这篇论文做了什么，不超过 40 个汉字。直接陈述内容，禁止出现"该摘要指出""本文章节""这一部分"这类叙述性前缀。
2. contributions 3–5 条，每条一句话，必须来自已给出的分节要点，直接说贡献本身。
3. argument_chain 写成"假设 → 方法 → 中间步骤 → 结论"的链条，4–7 步。
4. limitations 中同时包含作者自陈与读者可见的适用条件。
5. reading_guide 给出建议阅读顺序、可跳过的部分及理由。
6. 不要引入分节要点之外的信息。只输出 JSON。"""


def normalize_summary_payload(data: Any) -> dict[str, Any]:
    """Map the model's actual reply onto the summary shape.

    The endpoint returns its own key names ("summary", "key_contributions", …) and
    sometimes a bare string for a list field, so every lookup accepts aliases and
    coerces to the expected container.
    """
    if not isinstance(data, dict):
        return {}
    guide = pick(data, "reading_guide", "reading_order", "guide", default={})
    if not isinstance(guide, dict):
        guide = {"order": as_list(guide)}
    return {
        "one_line": str(pick(data, "one_line", "oneline", "tldr_line", "one_sentence",
                             "brief", default="")).strip(),
        "tldr": str(pick(data, "tldr", "summary", "overview", "abstract",
                         default="")).strip(),
        "contributions": [str(x).strip() for x in
                          as_list(pick(data, "contributions", "key_contributions",
                                       "novelty", default=[])) if str(x).strip()],
        "argument_chain": [str(x).strip() for x in
                           as_list(pick(data, "argument_chain", "logic_chain",
                                        "reasoning", default=[])) if str(x).strip()],
        "limitations": [str(x).strip() for x in
                        as_list(pick(data, "limitations", "limitation",
                                     "caveats", default=[])) if str(x).strip()],
        "reading_guide": {
            "order": [str(x).strip() for x in as_list(guide.get("order")) if str(x).strip()],
            "skip": [str(x).strip() for x in as_list(guide.get("skip")) if str(x).strip()],
            "why": str(guide.get("why") or guide.get("reason") or "").strip(),
        } if guide else {},
    }


_META_PREFIX = re.compile(
    r"^(该摘要指出[，,]?|摘要指出[，,]?|该(节|章|部分)(说明|介绍|列出|给出|总结|报告|讨论|展示|回顾|描述|是)[^，。；]{0,16}[，,，]?|"
    r"本(节|章|部分|文)(说明|介绍|列出|给出|总结|报告|讨论|展示|回顾|描述|包含|是)[^，。；]{0,16}[，,，]?|"
    r"这一?部分[^，。]{0,10}[，,]?|该部分[^，。]{0,10}[，,]?)"
)


def strip_meta(text: str) -> str:
    """Remove section-narration prefixes ("该摘要指出，…") model replies use.

    A reading entry should state the paper's point directly, not describe which
    part of the document it came from.
    """
    out = (text or "").strip()
    while True:
        m = _META_PREFIX.match(out)
        if not m:
            return out
        out = out[m.end():].lstrip("：:，, ")


def _first_sentence(text: str) -> str:
    text = strip_meta(text)
    for sep in ("。", "；", ". "):
        idx = text.find(sep)
        if idx != -1:
            return strip_meta(text[: idx + 1].strip())
    return text.strip()


def _core_order(digests: list[dict[str, Any]]) -> list[int]:
    """Prefer the canonical reading order when picking digests for the tldr."""
    wanted = ["abstract", "introduction", "method", "experiments", "results",
              "conclusion", "unknown"]
    by_kind = {d["kind"]: i for i, d in enumerate(digests)}
    order = []
    for kind in wanted:
        if kind in by_kind:
            order.append(by_kind[kind])
    order += [i for i in range(len(digests)) if i not in order]
    return order[:3]


class Summarizer:
    #: Points per section fed to the summary call. Six was the original figure;
    #: the digests already say most of it, and the model's reply is bounded by
    #: ``max_tokens`` anyway, so extra points mostly cost input.
    points_per_section: int = 3

    def __init__(self, backend: ModelBackend, *, points_per_section: int | None = None) -> None:
        self.backend = backend
        if points_per_section is not None:
            self.points_per_section = max(1, int(points_per_section))

    def build(
        self,
        *,
        title: str,
        abstract: str,
        extracts: dict[str, ModuleExtract],
        scope: str = "full",
    ) -> tuple[Summary, dict[str, Any]]:
        digests = [
            {"kind": ex.kind, "text": ex.digest}
            for ex in extracts.values()
            if ex.digest
        ]
        digest_text = "\n".join(f"- {d['kind']}：{d['text']}" for d in digests) or "（无）"
        points_text = "\n".join(
            f"- [{ex.kind}] {p['text']}"
            for ex in extracts.values()
            for p in ex.points[: self.points_per_section]
        ) or "（无）"

        user = (
            f"标题：{title}\n"
            f"摘要：{abstract[:1500] or '（无）'}\n\n"
            f"各节摘要：\n{digest_text}\n\n"
            f"各节要点：\n{points_text}\n\n"
            "请生成导读。"
        )
        truncated = False
        degraded = ""
        try:
            reply = call_json(self.backend, schema=SUMMARY_SCHEMA, system=SYSTEM,
                              user=user, max_tokens=12000, task="summarize")
            data = reply.data if reply.data is not None else {}
        except ModelTruncated as exc:
            # The reply ran out of room. Keep the deterministic fallback below so
            # the paper still has a headline, but say so: a cut-off summary used
            # to be recorded as a successful run with every list field empty.
            truncated = True
            reply = exc.reply
            data = {}
        except ModelSchemaError as exc:
            # Even after one retry the model would not produce the object. The
            # extracts are fine, so the paper is still worth having; the stitched
            # headline below carries it and the run is marked as degraded.
            degraded = str(exc)[:200]
            reply = getattr(exc, "reply", None)
            data = {}
        except ModelError as exc:
            raise ModelSchemaError(f"summary call failed: {exc}") from exc
        data = normalize_summary_payload(data)
        summary = Summary(
            one_line=str(data.get("one_line", "")).strip(),
            tldr=str(data.get("tldr", "")).strip(),
            contributions=[str(x).strip() for x in data.get("contributions", []) if str(x).strip()],
            section_digests=digests,
            argument_chain=[str(x).strip() for x in data.get("argument_chain", []) if str(x).strip()],
            limitations=[str(x).strip() for x in data.get("limitations", []) if str(x).strip()],
            reading_guide=data.get("reading_guide") or {},
            scope=scope,
        )
        # The gateway sometimes returns a summary shape no alias table matches.
        # Deriving the headline fields from the module digests is deterministic
        # and guarantees the reading entry is never blank when extraction worked.
        if not summary.one_line and digests:
            # Prefer the abstract/introduction digest over whatever comes first
            # in the file — the first entry is often front matter (title page,
            # author list), which makes a useless headline. No length cap: a
            # complete sentence beats a truncated one. Mark the origin: the
            # preview's model-written headline is better than this stitching.
            order = _core_order(digests)
            summary.one_line = _first_sentence(digests[order[0]]["text"])
            summary.reading_guide = summary.reading_guide or {}
            summary.one_line_source = "truncated" if truncated else "fallback"
        if not summary.tldr:
            picked = (digests[i] for i in _core_order(digests))
            summary.tldr = " ".join(
                _first_sentence(d["text"]) for d in picked if d["text"]
            )[:600]
        if scope == "abstract_only":
            summary.argument_chain = []
            summary.reading_guide = {}
        meta = reply.accounting() if reply is not None else {}
        if degraded:
            meta["error"] = degraded
        return summary, meta


def summary_markdown(summary: Summary) -> str:
    parts = ["## 一句话", summary.one_line or "（未生成）", ""]
    if summary.tldr:
        parts += ["## 五句概览", summary.tldr, ""]
    if summary.contributions:
        parts += ["## 贡献"] + [f"- {c}" for c in summary.contributions] + [""]
    if summary.section_digests:
        parts += ["## 分节要点"] + [
            f"- **{d['kind']}**：{d['text']}" for d in summary.section_digests
        ] + [""]
    if summary.argument_chain:
        parts += ["## 论证链"] + [f"{i}. {s}" for i, s in enumerate(summary.argument_chain, 1)] + [""]
    if summary.limitations:
        parts += ["## 局限"] + [f"- {x}" for x in summary.limitations] + [""]
    guide = summary.reading_guide or {}
    if guide:
        parts += ["## 阅读导引"]
        if guide.get("order"):
            parts.append("建议顺序：" + "；".join(guide["order"]))
        if guide.get("skip"):
            parts.append("可跳过：" + "；".join(guide["skip"]))
        if guide.get("why"):
            parts.append(f"理由：{guide['why']}")
    return "\n".join(parts)
