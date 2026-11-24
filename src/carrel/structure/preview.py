"""Fast preview: one cheap call that makes a paper readable within ~2 minutes.

Runs right after parsing, before the full per-section extraction. It only sees
the abstract and the introduction, and produces exactly what the overview card
and the reading entry need: one line, a short TLDR and the
contributions. The full summary later replaces it field by field.
"""

from __future__ import annotations

from typing import Any

from carrel.model import ModelBackend, ModelError
from carrel.model.base import as_list, call_json, pick

PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "one_line": {"type": "string"},
        "tldr": {"type": "string"},
        "contributions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["one_line"],
    "additionalProperties": False,
}

SYSTEM = """你在为一篇英文论文生成中文速览，供读者决定是否精读。规则：
1. 标题仅用于理解论文，不输出标题翻译或作者名单。
2. one_line 一句话说清这篇论文做了什么，直接陈述内容，禁止"该摘要指出""本文章节"这类叙述性前缀。
3. contributions 2–4 条，每条一句话，直接说贡献本身。
4. 只依据给出的摘要与引言，不要编造。只输出 JSON。"""


def build_preview(
    backend: ModelBackend,
    *,
    title: str,
    abstract: str,
    intro_text: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``({one_line, tldr, contributions}, run_meta)``; never raises.

    ``run_meta`` carries the call's accounting (and an ``error`` key when the
    call failed) so the pipeline can record the spend either way — this call
    used to be invisible in ``model_runs``.
    """
    meta: dict[str, Any] = {}
    try:
        reply = call_json(
            backend, schema=PREVIEW_SCHEMA, system=SYSTEM,
            user=(
                f"标题：{title or '（未知）'}\n\n"
                f"摘要：{abstract[:4000] or '（无）'}\n\n"
                f"引言节选：{intro_text[:4000] or '（无）'}"
            ),
            max_tokens=1500,
            task="preview",
        )
        meta = reply.accounting()
        data = reply.data if reply.data is not None else {}
    except ModelError as exc:
        if getattr(exc, "reply", None) is not None:
            meta = exc.reply.accounting()
        meta["error"] = f"{exc.code}: {exc}"[:200]
        return {}, meta
    if not isinstance(data, dict):
        return {}, meta
    preview = {
        "one_line": str(pick(data, "one_line", "oneline", "summary",
                             "brief", default="")).strip(),
        "tldr": str(pick(data, "tldr", "overview", "abstract",
                         default="")).strip(),
        "contributions": [str(x).strip() for x in
                          as_list(pick(data, "contributions", "key_contributions",
                                       default=[])) if str(x).strip()],
    }
    if not preview["one_line"]:
        return {}, meta
    return preview, meta
