"""Module-level translation with a deterministic repair loop.

One call per module (not per block) so the model sees the whole section and can
keep terminology, pronouns and cross-references consistent. Self-reported
cleanliness never overrides the deterministic checks in :mod:`carrel.translate.qa`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from carrel.model import ModelBackend, ModelError
from carrel.model.base import call_json
from carrel.translate.glossary import Glossary
from carrel.translate.qa import HARD_CHECKS, MATH_DELIM, QAReport, check_module

TRANSLATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["block_id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

REPAIR_SCHEMA = TRANSLATE_SCHEMA

SKIP_TYPES = {"formula", "figure"}

# ``HARD_CHECKS`` — the checks that justify spending another model call — lives
# in :mod:`carrel.translate.qa` next to the checks themselves.

BASE_SYSTEM = """你是技术论文翻译器，把英文原文译成简体中文，读者是需要精读该论文的研究者。

硬性规则：
1. 数字、百分号、小数、单位必须与原文完全一致，不得改写、四舍五入或换算。
2. 比较运算符（<、>、≤、≥、=）必须与原文一致。
3. 引文编号如 [12]、(Smith et al., 2024) 原样保留，不得增删或重排。
4. 公式、代码、变量名、数据集名、指标名、模型名保留英文原文。
5. 不增删句子，不合并或拆分段落；每个 block_id 对应一段译文。
6. 术语表里标注"保留原文不译"的词，必须原样保留。
7. 术语表中给出中文译法的词，全文必须使用同一译法。
8. 只输出 JSON。"""


@dataclass(slots=True)
class ModuleTranslation:
    kind: str
    translations: dict[str, str]
    qa: QAReport
    repair_rounds: int = 0
    runs: list[dict[str, Any]] = field(default_factory=list)
    initial_issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "translations": self.translations,
            "qa": self.qa.to_dict(),
            "repair_rounds": self.repair_rounds,
            "initial_issues": self.initial_issues,
        }


class Translator:
    def __init__(self, backend: ModelBackend, max_repair_rounds: int = 1) -> None:
        self.backend = backend
        self.max_repair_rounds = max_repair_rounds

    # --------------------------------------------------------------- public
    def translate_module(
        self,
        *,
        kind: str,
        blocks: list[dict[str, Any]],
        glossary: Glossary,
        context: str = "",
    ) -> ModuleTranslation:
        payload = _render_blocks(blocks)
        user = (
            f"上下文（上一节末尾与论文一句话摘要）：\n{context or '（无）'}\n\n"
            f"术语表：\n{glossary.as_prompt_table()}\n\n"
            f"待翻译原文（每行以 [block_id] 开头）：\n{payload}\n\n"
            "请输出每个 block_id 对应的中文译文。公式与图注块可原样保留原文。"
        )
        # The gateway sometimes flattens the envelope: a bare list of items, or
        # a single {block_id, text} object at the top level. Parse without
        # enforcing the schema, then normalise locally — same lesson as extract.
        reply = call_json(self.backend, schema=TRANSLATE_SCHEMA, system=BASE_SYSTEM,
                          user=user, max_tokens=24000, task="translate")
        translations = _collect(_normalize_translations(reply.data), blocks)
        runs = [{"task": "translate", "module": kind, "status": "ok", **reply.accounting()}]

        report = self._check(blocks, translations, glossary)
        initial_issues = [i.to_dict() for i in report.issues]
        rounds = 0
        while rounds < self.max_repair_rounds:
            # Only hard errors justify another model call. Soft flags (length
            # ratio, residual English on short blocks, glossary drift) annotate
            # the block for review instead — repairing them cost ~35% of run time
            # while the final residual issues were all soft anyway.
            failing = {i.block_id for i in report.issues if i.check in HARD_CHECKS}
            if not failing:
                break
            rounds += 1
            translations, run, report = self._repair(
                kind=kind,
                all_blocks=blocks,
                failing=failing,
                translations=translations,
                report=report,
                glossary=glossary,
                context=context,
            )
            runs.append(run)

        return ModuleTranslation(
            kind=kind, translations=translations, qa=report,
            repair_rounds=rounds, runs=runs,
            initial_issues=initial_issues,
        )

    # -------------------------------------------------------------- repair
    def _repair(self, *, kind, all_blocks, failing, translations, report,
                glossary, context):
        targets = [b for b in all_blocks if b["id"] in failing]
        failures: list[str] = []
        for block in targets:
            # Only the hard failures are worth the model's attention; a length
            # ratio or a residual English run is a review flag, and asking the
            # model to "fix" those invites rewrites of healthy sentences.
            issues = [i for i in report.issues_for(block["id"]) if i.check in HARD_CHECKS]
            if not issues:
                continue
            detail = "；".join(f"{i.check}: {i.detail}" for i in issues)
            failures.append(
                f"[{block['id']}]\n原文：{clean_source(block['text'])}\n当前译文：{translations.get(block['id'], '')}\n"
                f"未通过检查：{detail}"
            )
        neighbours = _neighbour_context(targets)
        # Only the terms that occur in the failing blocks: the whole table is
        # noise here and was the largest fixed cost of every repair prompt.
        local_glossary = glossary.subset_for(" ".join(b["text"] for b in targets))
        user = (
            f"术语表：\n{local_glossary.as_prompt_table()}\n\n"
            f"上下文：\n{context or '（无）'}\n{neighbours}\n\n"
            "以下译文的自动检查未通过，请只修正这些块并重新输出它们：\n\n"
            + "\n\n".join(failures)
        )
        try:
            # Same tolerant path as the first pass: the gateway flattens the
            # envelope here too, and a strict check would kill the module even
            # though the first-pass translation is already on disk.
            reply = call_json(self.backend, schema=REPAIR_SCHEMA, system=BASE_SYSTEM,
                              user=user, max_tokens=16000, task="repair")
        except ModelError as exc:
            # A transient failure during repair must not discard the complete
            # first pass; keep it, flag the blocks, and move on.
            run = {
                "task": "repair", "module": kind, "status": "error",
                "error_code": exc.code, "detail": str(exc)[:200], "accepted": False,
            }
            partial = getattr(exc, "reply", None)
            if partial is not None:
                run.update(partial.accounting())
            return dict(translations), run, report

        reply.data = _normalize_translations(reply.data)

        merged = dict(translations)
        changed: list[str] = []
        for item in (reply.data or {}).get("translations", []):
            if not isinstance(item, dict):
                continue
            bid = str(item.get("block_id") or item.get("id") or item.get("block") or "")
            # Only blocks that were actually failing may be replaced. Accepting a
            # rewrite of a healthy block lets one bad repair corrupt good output.
            if bid in failing and bid in merged:
                new_text = str(item.get("text") or item.get("translation") or item.get("zh") or "")
                if new_text and new_text != merged[bid]:
                    merged[bid] = new_text
                    changed.append(bid)

        candidate = self._check(all_blocks, merged, glossary)

        # A repair is judged block by block: a rewrite is kept only where it
        # strictly reduces that block's hard issues, and rolled back where it
        # does not. Module-level counting let one block's improvement pay for
        # another block's regression, so a complete paragraph could be swapped
        # for a broken one and still count as "better". Soft flags are excluded
        # on purpose: fixing a flipped inequality but leaving one English term
        # is a win.
        before_by = _hard_by_block(report)
        after_by = _hard_by_block(candidate)
        kept: list[str] = []
        for bid in changed:
            if after_by.get(bid, 0) < before_by.get(bid, 0):
                kept.append(bid)
            else:
                merged[bid] = translations[bid]
        if kept and len(kept) < len(changed):
            candidate = self._check(all_blocks, merged, glossary)
        elif not kept:
            candidate = report
        accepted = bool(kept)
        before_issues = _hard_count(report)
        after_issues = _hard_count(candidate)

        run = {
            "task": "repair", "module": kind,
            "status": "ok" if accepted else "rejected",
            "accepted": accepted,
            "before_issues": before_issues, "after_issues": after_issues,
            "blocks_changed": len(changed), "blocks_kept": len(kept),
            **reply.accounting(),
        }
        return merged, run, candidate

    def _check(self, blocks, translations, glossary) -> QAReport:
        return check_module(
            source_blocks=blocks,
            translations=translations,
            skip_types=SKIP_TYPES,
            glossary_terms=glossary.as_terms_map(),
        )


def _hard_count(report: QAReport) -> int:
    return sum(1 for i in report.issues if i.check in HARD_CHECKS)


def _hard_by_block(report: QAReport) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in report.issues:
        if issue.check in HARD_CHECKS:
            counts[issue.block_id] = counts.get(issue.block_id, 0) + 1
    return counts


_ID_LIKE = re.compile(r"^[A-Za-z]+\d+-[A-Za-z]+\d+$|^p\d+-b\d+$")


def _normalize_translations(data: Any) -> Any:
    """Map whatever shape the gateway returned onto the expected envelope.

    Seen in practice: the canonical ``{"translations": [{block_id, text}]}``, a
    bare list of items, a single item at the top level, ``{"data": [...]}``
    wrappers — and, from glm-5.3, a flat ``{"p4-b3": "译文", "p4-b4": "..."}``
    map keyed by block id. That last one read as *zero* translations and sent
    every block to repair, which is the most expensive way to be right.
    """
    if isinstance(data, dict) and isinstance(data.get("translations"), list):
        return data
    if isinstance(data, dict) and isinstance(data.get("translations"), dict):
        data = data["translations"]
    if isinstance(data, list):
        return {"translations": data}
    if isinstance(data, dict) and "block_id" in data:
        return {"translations": [data]}
    if isinstance(data, dict):
        # {"data": [...]} / {"results": [...]} style wrappers
        for key in ("data", "results", "items", "output"):
            value = data.get(key)
            if isinstance(value, list):
                return {"translations": value}
        # flat {block_id: text} map
        if data and all(isinstance(v, str) for v in data.values()) and \
                sum(1 for k in data if _ID_LIKE.match(str(k))) >= max(1, len(data) // 2):
            return {"translations": [{"block_id": str(k), "text": v} for k, v in data.items()]}
    return data


def _collect(data: Any, blocks: list[dict[str, Any]]) -> dict[str, str]:
    wanted = {b["id"] for b in blocks}
    out: dict[str, str] = {}
    for item in (data or {}).get("translations", []):
        if not isinstance(item, dict):
            continue
        bid = str(item.get("block_id") or item.get("id") or item.get("block") or "").strip()
        if bid in wanted:
            out[bid] = str(item.get("text") or item.get("translation") or item.get("zh") or "")
    # Blocks the model silently skipped are filled with the source text so the
    # QA pass can report them precisely instead of cascading.
    for block in blocks:
        out.setdefault(block["id"], "")
    return out


def _render_blocks(blocks: list[dict[str, Any]]) -> str:
    lines = []
    for block in blocks:
        flag = " (公式，可原样保留)" if block.get("type") == "formula" else ""
        lines.append(f"[{block['id']}]{flag} {clean_source(block.get('text', ''))}")
    return "\n".join(lines)


_BROKEN_HYPHEN = re.compile(r"(\w)-\s+(\d)")
# Text-mode arguments whose interior spaces are content, not MinerU noise.
_TEXT_ARG = re.compile(r"(\\(?:text|mathrm|mathbf|mathit|operatorname|mbox|textbf|textit))\s*\{([^{}]*)\}")


def clean_source(text: str) -> str:
    """What the model is shown, as opposed to what the reader stores.

    MinerU emits ``$1 0 ^ { - 6 }$`` for ``$10^{-6}$`` and leaves ``Qwen2.5- 1.5B``
    at a line break. A good translation fixes both — and then the QA check,
    comparing against the raw source, used to flag the fix. Cleaning the prompt
    side removes the temptation to copy the damage; the QA side normalises the
    same way (:mod:`carrel.translate.qa`, which shares the ``$`` pairing), so
    the two agree. The stored source text is untouched.
    """
    t = text or ""
    parts = MATH_DELIM.split(t)
    delims = MATH_DELIM.findall(t)
    if len(parts) >= 3:
        out: list[str] = []
        for i, part in enumerate(parts):
            if i % 2 == 1 and i < len(parts) - 1:
                out.append(_tidy_math(part))
            else:
                out.append(part)
            if i < len(delims):
                out.append(delims[i])
        t = "".join(out)
    t = _BROKEN_HYPHEN.sub(r"\1-\2", t)
    return t


def _tidy_math(inner: str) -> str:
    # MinerU puts a space between every character; there is no information in
    # that spacing, so drop all of it and re-introduce the few spaces that keep
    # a formula readable: around relations, after list commas, and between a
    # command name and a following letter (``\mid x`` must not become ``\midx``).
    # Two constructs carry real spaces and are protected whole: the ``\\`` row
    # separator (a single-backslash-space pass would eat half of it and fuse
    # the rest with the next token) and ``\text{ for all }``-style arguments.
    protected: list[str] = []

    def keep(s: str) -> str:
        protected.append(s)
        return f"\x02{len(protected) - 1}\x03"

    s = inner.replace("\\\\", keep("\\\\"))
    s = _TEXT_ARG.sub(lambda m: keep(m.group(1) + "{" + re.sub(r"\s+", " ", m.group(2).strip()) + "}"), s)
    s = re.sub(r"(?<!\\)\\ ", "", s)
    s = re.sub(r"(\\[A-Za-z]+)\s+(?=[A-Za-z])", lambda m: m.group(1) + "\x00", s)
    s = re.sub(r"\s+", "", s)
    s = s.replace("\x00", " ")
    s = re.sub(r"([=<>≤≥])", r" \1 ", s)
    s = re.sub(r",(?=\S)", ", ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\x02(\d+)\x03", lambda m: " " + protected[int(m.group(1))] + " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _neighbour_context(blocks: list[dict[str, Any]]) -> str:
    if not blocks:
        return ""
    first, last = blocks[0], blocks[-1]
    return f"相邻块：前为 [{first['id']}]，后为 [{last['id']}]。"
