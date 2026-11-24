"""Deterministic translation checks.

These never judge prose quality. They answer one question: did the translation
introduce a mechanical error that a human skimming 40 pages would miss? The
number check is the highest-value one, because a dropped percent sign or a
flipped inequality reads perfectly and is wrong.

Both sides are normalised the same way before anything is compared. On real
papers most "errors" the old regexes reported were artefacts of the source —
MinerU spaces out every character inside ``$...$`` (``$1 0 ^ { - 6 }$``), a
line break leaves ``Qwen2.5- 1.5B``, a Unicode minus sits where ASCII ``-`` was
expected — and a translation that *fixed* them was punished for it. Nine out of
ten repair calls were spent that way, and an accepted repair sometimes copied
the damage back in to pass the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Checks that justify spending another model call on a block. A flipped
#: inequality or a wrong number is a factual error the reader cannot see; a
#: short paragraph or an untranslated heading is cosmetic and gets flagged.
HARD_CHECKS = frozenset({"numbers", "citations", "relations", "missing", "extra", "empty"})

# A number is a run of digits with optional sign, decimal part, and an attached
# percent or multiplier. The sign matters: "-12.5" must not quietly become
# "12.5", and "3.2x"/"3.2×" must not lose its multiplier. The lookbehind still
# rejects digits inside words and hyphenated names ("COVID-19" contributes
# nothing because the char before "-" is a letter). A leading-dot decimal
# (".018", the way p-values are printed) is a number too. A comma is a
# thousands separator only when exactly three digits follow: "12，34 分" (two
# values with a full-width comma, folded to ASCII below) must stay two values.
_NUM = re.compile(r"(?<![A-Za-z0-9_.\-%])[+\-]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)\s*[×%‰]?")
# Normalise the ways "×" appears in source and translation so they compare equal.
# The symbol forms (``3.2x``, ``3.2×``) and the Chinese 倍 all become ``×``.
# The *word* "times" is left alone: "3 times" is a repetition count as often
# as a multiplier and 次 is not a multiplier at all, so the word form on
# either side contributes the bare number, and "3 times"/"3 次" compare equal
# while "30×"/"30 倍" also compare equal.
_TIMES_X = re.compile(r"(\d)\s*[xX](?![A-Za-z])")
_TIMES_WORD = re.compile(r"(\d)\s*-?\s*(?:times|fold)\b", re.I)
_TIMES_ZH = re.compile(r"(\d)\s*倍")
# "23 to 24%" / "23–24%": the percent sign on the range's end applies to both
# bounds. The separator is *replaced*, not kept: "23%-24%" would otherwise read
# as "23%" and "-24%". A bound that follows a sign or another percent is not a
# range start ("-0.17 -3%" in a table is two deltas).
_PCT_RANGE = re.compile(
    r"(?<![\d.%‰+\-])(\d+(?:\.\d+)?)%?"
    r"(?:\s*(?:to|and|到|至|~|～)\s*|\s*[-–—]\s*)"
    r"(\d+(?:\.\d+)?%)(?!\d)"
)
_CJK = re.compile(r"[一-鿿]")
_CITE_BRACKET = re.compile(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]")
_CITE_PAREN = re.compile(r"\([^()]{0,80}?(?:et al\.|,)\s*\d{4}[a-z]?\)")
_EQUATION = re.compile(r"[<>≤≥=≈≠]")
# A run of Latin words long enough to be prose rather than a proper noun.
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z\-]*(?:[ ,][A-Za-z][A-Za-z\-]*){4,}")

# --- source/target normalisation (applied symmetrically) -------------------
# ``$...$`` spans are paired by parity, not by a bounded regex: a formula that
# happened to exceed a length cap was treated as prose on one side and maths
# on the other, and the verdict flipped on formula length alone.
MATH_DELIM = re.compile(r"\$+")
# Any word character before the hyphen, not just a digit: "ResNet- 50" is the
# same line-break damage as "Qwen2.5- 1.5B" and must normalise the same way
# on the QA side as :func:`carrel.translate.translator.clean_source` does on
# the prompt side, or a correct translation can never pass.
_BREAK_HYPHEN = re.compile(r"(\w)-\s+(\d)")
_FULLWIDTH = str.maketrans({
    "（": "(", "）": ")", "［": "[", "］": "]", "，": ",", "；": ";", "：": ":",
    "−": "-", "–": "-", "—": "-", "％": "%",
})
# LaTeX spellings of the suffixes the prose regex understands.
_LATEX_SUFFIX = (
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\permil\b"), "‰"),
    # ``\times`` is a multiplier suffix only when no number follows; in
    # ``5\times10^{-5}`` it is scientific notation and must stay a separator.
    (re.compile(r"\\times(?![\s{]*\d)"), "×"),
)


def normalize_for_compare(text: str) -> str:
    """Canonical form for the mechanical checks. Never shown to the reader.

    Punctuation width, the broken hyphen and the percent range are fixed here;
    maths is handled separately by :func:`numbers_in` so that the whitespace
    MinerU inserts inside ``$...$`` cannot turn one number into three. Number
    *words* are deliberately left alone on both sides: "twelve" and "十二"
    contribute no digit, so a word-for-word rendering compares equal, and
    mapping Chinese numerals would misfire on 一个/第一/一次.
    """
    t = text or ""
    t = t.translate(_FULLWIDTH)
    t = _BREAK_HYPHEN.sub(r"\1-\2", t)
    t = _PCT_RANGE.sub(lambda m: f"{m.group(1)}% {m.group(2)}", t)
    return t


def split_math(text: str) -> tuple[str, list[str]]:
    """Return ``(prose with maths blanked, [maths spans, whitespace collapsed])``.

    Spans are delimited by ``$`` parity so a long formula is handled the same
    way as a short one. Inside a span the whitespace MinerU inserts between
    every character is collapsed (``$1 0 ^ { - 6 }$`` -> ``$10^{-6}$``). An
    unpaired trailing ``$`` is left in the prose.
    """
    parts = MATH_DELIM.split(text)
    if len(parts) < 3:
        return text, []
    prose: list[str] = []
    spans: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1 and i < len(parts) - 1:
            spans.append(re.sub(r"\s+", "", part))
            prose.append(" ")
        else:
            prose.append(part)
    return "".join(prose), spans


@dataclass(slots=True)
class QAIssue:
    block_id: str
    check: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"block_id": self.block_id, "check": self.check, "detail": self.detail}


@dataclass(slots=True)
class QAReport:
    issues: list[QAIssue] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def failed_blocks(self) -> set[str]:
        return {i.block_id for i in self.issues}

    def issues_for(self, block_id: str) -> list[QAIssue]:
        return [i for i in self.issues if i.block_id == block_id]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checked": self.checked,
                "issues": [i.to_dict() for i in self.issues]}


# Inside maths a number may sit directly after a command name (``\times10``,
# ``\mathrm{was-0.020``), so the lookbehind only refuses digits and dots — a
# letter before the number is a LaTeX token boundary, not part of an identifier.
_MATH_NUM = re.compile(r"(?<![\d.])[+\-]?(?:\d+(?:\.\d+)?|\.\d+)\s*[×%‰]?")
# ``x^{2}``, ``\alpha_1`` — an unsigned digit glued to an identifier via ^ or _
# is a super/subscript index, not a value. A *signed* exponent (``10^{-6}``)
# carries the value and is kept because the pattern requires a bare digit run.
_MATH_SCRIPT = re.compile(r"[\^_]\{?\d+\}?")


def _math_numbers(spans: list[str]) -> list[str]:
    """Numbers inside maths, with the same suffix vocabulary as prose.

    Commas inside maths are list separators (``$k = 1, 2$``), never thousands
    separators, so the pattern is stricter than :data:`_NUM`; LaTeX suffix
    spellings are folded first so ``$98.5\\%$`` and ``98.5%`` compare equal.
    """
    out: list[str] = []
    for span in spans:
        for pat, repl in _LATEX_SUFFIX:
            span = pat.sub(repl, span)
        span = _MATH_SCRIPT.sub("", span)
        out.extend(re.sub(r"\s+", "", n) for n in _MATH_NUM.findall(span))
    return out


def numbers_in(text: str) -> list[str]:
    prose, spans = split_math(normalize_for_compare(text))
    # Word-form multipliers ("30 times", "3-fold") carry no marker — the word is
    # ambiguous with a repetition count; symbol forms and 倍 gain the canonical
    # ×. check_block reconciles the two spellings.
    prose = _TIMES_WORD.sub(r"\1", prose)
    prose = _TIMES_ZH.sub(r"\1×", _TIMES_X.sub(r"\1×", prose))
    out = []
    for raw in _NUM.findall(prose):
        # Strip the thousands separator and any whitespace (a table cell can
        # leave a newline glued to the digit); keep the sign and the suffix.
        norm = re.sub(r"[,\s]", "", raw)
        if norm in ("+", "-", ""):
            continue
        out.append(norm)
    out.extend(_math_numbers(spans))
    return sorted(out)


def _strip_multiplier(values: list[str]) -> list[str]:
    """``30×`` -> ``30``; used to compare multipliers leniently."""
    return sorted(v[:-1] if v.endswith("×") else v for v in values)


def cites_in(text: str) -> list[str]:
    text = normalize_for_compare(text)
    found = _CITE_BRACKET.findall(text) + _CITE_PAREN.findall(text)
    return sorted("".join(x.split()) for x in found)


def _counts(values: list[str]) -> str:
    from collections import Counter

    return "、".join(f"{k}×{v}" for k, v in sorted(Counter(values).items())) or "无"


def relations_in(text: str) -> list[str]:
    """Comparison operators, so ``p<0.05`` cannot become ``p>0.05`` silently."""
    return sorted(_EQUATION.findall(normalize_for_compare(text)))


def cjk_ratio(text: str) -> float:
    stripped = re.sub(r"\s", "", text or "")
    if not stripped:
        return 0.0
    return len(_CJK.findall(stripped)) / len(stripped)


def check_block(
    *,
    block_id: str,
    source: str,
    target: str,
    translate_required: bool,
    glossary_terms: dict[str, str] | None = None,
    block_type: str = "text",
) -> list[QAIssue]:
    issues: list[QAIssue] = []
    if not target.strip() and translate_required:
        return [QAIssue(block_id, "empty", "译文为空")]

    src_nums = numbers_in(source)
    tgt_nums = numbers_in(target)
    # "30 times" vs "30 倍": the word form carries no marker and 倍 does. When
    # the lists differ only by × suffixes, treat them as equal — the multiplier
    # was rendered, just spelled differently. A *dropped* marker against a
    # symbol source ("3.2x" -> "3.2") still shows up because the symbol form
    # on the source side keeps its ×.
    if src_nums != tgt_nums and _strip_multiplier(src_nums) == _strip_multiplier(tgt_nums) \
            and _TIMES_WORD.search(normalize_for_compare(source)):
        tgt_nums = src_nums
    if block_type in ("table", "figure"):
        # A table or figure caption is layout, not prose: merged cells repeat a
        # value and row labels get reordered. Extra repetitions on the target
        # side are tolerated; a value that appears *fewer* times than in the
        # source (a dropped row, a collapsed column) is still an error.
        from collections import Counter

        src_c, tgt_c = Counter(src_nums), Counter(tgt_nums)
        deficient = sorted(k for k, v in src_c.items() if tgt_c[k] < v)
        if deficient:
            issues.append(QAIssue(block_id, "numbers",
                                  "原文有而译文缺：" + "、".join(deficient[:6])))
    elif src_nums != tgt_nums:
        missing = [n for n in src_nums if n not in tgt_nums]
        extra = [n for n in tgt_nums if n not in src_nums]
        detail: list[str] = []
        if missing:
            detail.append("原文有而译文缺：" + "、".join(missing[:6]))
        if extra:
            detail.append("译文多出：" + "、".join(extra[:6]))
        if not detail:
            # Same values but different multiplicity, e.g. "12" repeated twice.
            detail.append(
                f"数值出现次数不一致：原文 {_counts(src_nums)}，译文 {_counts(tgt_nums)}"
            )
        issues.append(QAIssue(block_id, "numbers", "；".join(detail)))

    src_cites = cites_in(source)
    tgt_cites = cites_in(target)
    if src_cites != tgt_cites:
        issues.append(QAIssue(
            block_id, "citations",
            f"引文编号不一致：原文 {src_cites[:5]} 译文 {tgt_cites[:5]}"))

    src_rel = relations_in(source)
    tgt_rel = relations_in(target)
    if src_rel != tgt_rel:
        issues.append(QAIssue(
            block_id, "relations",
            f"比较符号不一致：原文 {src_rel} 译文 {tgt_rel}"))

    if translate_required:
        ratio = cjk_ratio(target)
        if ratio < 0.15:
            issues.append(QAIssue(block_id, "cjk_ratio",
                                  f"中文占比仅 {ratio:.0%}，疑似漏译"))
        # Length ratio is only meaningful once there is enough text to compare.
        # Headings and short labels legitimately compress hard ("Abstract" ->
        # "摘要"), so gating on a minimum source length avoids constant noise.
        if len(source) >= 40:
            length_ratio = len(target) / max(1, len(source))
            if not (0.35 <= length_ratio <= 2.6):
                issues.append(QAIssue(
                    block_id, "length_ratio",
                    f"长度比 {length_ratio:.2f} 超出 0.35–2.6"))

        # Untranslated prose: five or more consecutive Latin words. Isolated
        # technical terms (Transformer, SWE-bench, ReAct) are correct Chinese
        # practice and must not be flagged — a competent translation keeps them.
        prose_runs = _LATIN_RUN.findall(target)
        if prose_runs:
            sample = prose_runs[0].strip()[:60]
            issues.append(QAIssue(
                block_id, "residual_english",
                f"含 {len(prose_runs)} 段连续英文（≥5 词），例如「{sample}」"))

    if glossary_terms:
        low_src = (source or "").lower()
        for src_term, zh in glossary_terms.items():
            if not src_term or zh in ("", "原文"):
                continue
            if not _term_present(low_src, src_term):
                continue
            if zh in target:
                continue
            # Renderings that are mostly Latin (e.g. "RAG") legitimately stay in
            # the translation unchanged, so only pure-CJK renderings are enforced.
            if any(ch.isascii() and ch.isalpha() for ch in zh):
                continue
            issues.append(QAIssue(
                block_id, "glossary",
                f"术语「{src_term}」未使用约定译法「{zh}」"))
    return issues


def _term_present(low_source: str, term: str) -> bool:
    """Whole-word presence, so ``router`` does not match ``routerbench``."""
    if not term:
        return False
    start = 0
    while True:
        idx = low_source.find(term, start)
        if idx == -1:
            return False
        before = low_source[idx - 1] if idx > 0 else ""
        after = low_source[idx + len(term)] if idx + len(term) < len(low_source) else ""
        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
            return True
        start = idx + 1


def check_module(
    *,
    source_blocks: list[dict[str, Any]],
    translations: dict[str, str],
    skip_types: set[str],
    glossary_terms: dict[str, str] | None = None,
) -> QAReport:
    report = QAReport()
    src_ids = [b["id"] for b in source_blocks]
    missing = [bid for bid in src_ids if bid not in translations]
    extra = [bid for bid in translations if bid not in set(src_ids)]
    for bid in missing:
        report.issues.append(QAIssue(bid, "missing", "缺少该块的译文"))
    for bid in extra:
        report.issues.append(QAIssue(bid, "extra", "出现原文中不存在的块"))

    for block in source_blocks:
        bid = block["id"]
        if bid not in translations:
            continue
        required = block.get("type") not in skip_types
        report.issues.extend(
            check_block(
                block_id=bid,
                source=block.get("text", ""),
                target=translations.get(bid, ""),
                translate_required=required,
                glossary_terms=glossary_terms,
                block_type=str(block.get("type") or "text"),
            )
        )
    report.checked = len(src_ids)
    return report
