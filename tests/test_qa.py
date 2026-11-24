"""Translation QA against the false positives seen on real papers.

Every "false positive" case here is a (source, translation) pair that a real
repair call was spent on, where a human reader sees no error. Every "real
error" case is a mechanical mistake the check exists to catch. Normalisation
must remove the former without losing any of the latter.
"""

from __future__ import annotations

import pytest

from carrel.translate.qa import HARD_CHECKS, check_block, cites_in, numbers_in


def hard(source: str, target: str, **kw) -> list[str]:
    issues = check_block(block_id="b", source=source, target=target,
                         translate_required=True, **kw)
    return sorted(i.check for i in issues if i.check in HARD_CHECKS)


# --------------------------------------------------------------------------
# False positives from real traces: the translation is right, the regex was not
# --------------------------------------------------------------------------

FALSE_POSITIVES = [
    pytest.param(
        # MinerU spaces out every character inside $...$ in the *source*.
        r"At $k = 4 .$ the rate is $1 0 ^ { - 6 }$ and Holm $p = . 0 1 8 )$.",
        "在 $k = 4$ 时速率为 $10^{-6}$，Holm $p = .018$。",
        id="spaced-latex-source",
    ),
    pytest.param(
        "dynamic sampling (Gao et al., 2026; Yu et al., 2025) helps.",
        "动态采样（Gao et al., 2026; Yu et al., 2025）有帮助。",
        id="fullwidth-citation-parens",
    ),
    pytest.param(
        "Earlier work [12] and [3] agree.",
        "更早的工作［12］与［3］一致。",
        id="fullwidth-citation-brackets",
    ),
    pytest.param(
        "KL is about 30 times that of the low-weight run.",
        "KL 约为低权重运行的 30 倍。",
        id="times-word-to-bei",
    ),
    pytest.param(
        "a gain of 23 to 24% on MATH",
        "在 MATH 上提升 23% 到 24%",
        id="percent-range-one-end",
    ),
    pytest.param(
        "on Qwen2.5- 1.5B the gain holds",
        "在 Qwen2.5-1.5B 上该增益成立",
        id="line-break-hyphen-in-source",
    ),
    pytest.param(
        "it peaks at three-sixteenths of the budget and saves about a fifth",
        "它在预算的十六分之三处达到峰值，约省下五分之一",
        id="english-number-words",
    ),
    pytest.param(
        "the difference is −0.03 [−1.3, +1.2] points",
        "差异为 -0.03 [-1.3, +1.2] 个点",
        id="unicode-minus",
    ),
    pytest.param(
        "| model | score |\n| base | 42.1 |\n| ours | 48.6 |",
        "| 模型 | 分数 |\n| 基线 | 42.1 |\n| 本文 | 48.6 |",
        id="table-cells-with-newlines",
    ),
    pytest.param(
        "Results on 1,543 problems and 1,319 GSM8K items.",
        "在 1,543 道题与 1,319 道 GSM8K 题上的结果。",
        id="thousands-separator",
    ),
    # --- from the adversarial review of the first normalisation pass ---
    pytest.param(
        "We score 12 and 34 points on the two benchmarks.",
        "我们在两个基准上分别得到 12，34 分。",
        id="fullwidth-comma-between-integers",
    ),
    pytest.param(
        "We repeat each evaluation 3 times and report the mean.",
        "我们将每次评估重复 3 次，并报告平均值。",
        id="times-as-repetition-count",
    ),
    pytest.param("a 3-fold speedup", "3 倍加速", id="hyphenated-fold"),
    pytest.param(
        "the gap widens for 23 to 24% of examples",
        "在 23%–24% 的例子上差距扩大",
        id="percent-range-dash-target",
    ),
    pytest.param("improves by 23% to 24%", "提升 23%–24%", id="percent-range-both-ends"),
    pytest.param(
        "| Fixed | Router | Oracle | 71.43 | -0.17 | -3% |",
        "| 固定 | 路由器 | 先知 | 71.43 | -0.17 | -3% |",
        id="signed-table-deltas-are-not-a-range",
    ),
    pytest.param("evaluated on ResNet- 50 and GPT- 4", "在 ResNet-50 与 GPT-4 上评估", id="letter-hyphen-break"),
    pytest.param(r"accuracy reaches $98.5\%$ on dev", "准确率在验证集上达到 98.5%", id="latex-percent-leaves-math"),
    pytest.param(r"it is $3\times$ faster", "它快 3 倍", id="latex-times-leaves-math"),
    pytest.param(r"with $p = 5\times10^{-5}$", r"其中 $p = 5\times10^{-5}$", id="scientific-notation-kept"),
    pytest.param(
        "$" + r"\mathcal{L}(\theta) = \sum_{i=1}^{N} " + " + ".join(rf"\alpha_{{{k}}} \ell_{{{k}}}(x_i, y_i)" for k in range(12)) + "$ and 0.25 overall",
        "$" + r"\mathcal{L}(\theta) = \sum_{i=1}^{N} " + " + ".join(rf"\alpha_{{{k}}} \ell_{{{k}}}(x_i, y_i)" for k in range(12)) + "$，整体 0.25",
        id="long-formula-no-length-cliff",
    ),
]


@pytest.mark.parametrize("source,target", FALSE_POSITIVES)
def test_correct_translation_is_not_flagged(source, target):
    assert hard(source, target) == []


# --------------------------------------------------------------------------
# Real errors: normalisation must not hide any of these
# --------------------------------------------------------------------------

REAL_ERRORS = [
    pytest.param("accuracy rises by 35.9%", "准确率上升 35.9", "numbers", id="dropped-percent"),
    pytest.param("significant with p<0.05", "在 p>0.05 时显著", "relations", id="flipped-inequality"),
    pytest.param("a gain of 1.9 points", "提升 1.7 个点", "numbers", id="changed-number"),
    pytest.param("as shown earlier (Gao et al., 2026; Yu et al., 2025) and [12]",
                 "如前所示 [12]", "citations", id="dropped-citation"),
    pytest.param("trained on 8 prompts", "在 8 个提示和 16 个样本上训练", "numbers", id="extra-number"),
    pytest.param("35.9% of cases", "35.9‰ 的情况", "numbers", id="percent-to-permille"),
    pytest.param("about 30× faster", "快约 30", "numbers", id="dropped-multiplier"),
    pytest.param(r"rate $10^{-6}$", r"速率 $10^{6}$", "numbers", id="sign-in-exponent"),
    pytest.param("| ours | 85.77 | +6.33 |", "| 本文 | 85.77 | +6.35 |", "numbers", id="table-cell-value"),
    pytest.param("on 1,543 problems", "在 1,54 道题上", "numbers", id="thousands-digit-lost"),
    # --- from the adversarial review ---
    pytest.param(r"accuracy reaches $9.8\%$", r"准确率达到 $9.8$", "numbers", id="latex-percent-dropped"),
    pytest.param("for 23-24% of cases", "在 23%-25% 的情况下", "numbers", id="percent-range-upper-changed"),
    pytest.param("12 and 34 points", "12，35 分", "numbers", id="fullwidth-comma-value-changed"),
]


@pytest.mark.parametrize("source,target,check", REAL_ERRORS)
def test_real_error_is_still_caught(source, target, check):
    assert check in hard(source, target), hard(source, target)


# --------------------------------------------------------------------------
# Unit behaviour of the normalised extractors
# --------------------------------------------------------------------------

class TestReplyShapes:
    """Envelopes seen from real models; each one once cost a repair round."""

    def test_latex_backslashes_inside_json_strings(self):
        # glm writes "\$0.21" and "\{1, 0.5\}" inside JSON strings. json.loads
        # rejects those; the whole 9-paragraph reply was thrown away and every
        # block went to repair. A backslash that starts no legal escape means a
        # literal backslash. (Legal escapes such as \t and \n keep their JSON
        # meaning — that is what a parser must do, and the real replies spell
        # "\\theta" when they mean theta.)
        from carrel.model import extract_json

        data = extract_json(r'{"p1": "costs \$0.21, with $\\theta\in\{1, 0.5\}$ and \left[ x \\right]"}', expect="object")
        assert data["p1"] == r"costs \$0.21, with $\theta\in\{1, 0.5\}$ and \left[ x \right]"
        # legal escapes are untouched
        assert extract_json(r'{"a": "line\nbreak \"quoted\" \\ backslash é"}')["a"] == 'line\nbreak "quoted" \\ backslash é'

    def test_stray_empty_braces_are_not_the_reply(self):
        # A "\tag{2}" inside the first paragraph made "{}" the first balanced
        # object; the scanner returned it and nine translations became none.
        from carrel.model import extract_json

        text = 'noise {} more noise {"p1": "甲", "p2": "乙"}'
        assert extract_json(text, expect="object") == {"p1": "甲", "p2": "乙"}
        # but a genuinely empty reply object is still an object
        assert extract_json("{}", expect="object") == {}

    def test_flat_block_id_map_is_a_translation(self):
        # glm-5.3 returns {"p4-b3": "译文", ...}. Read as zero translations, this
        # flagged all 23 blocks "empty" and sent every one of them to repair.
        from carrel.translate.translator import _collect, _normalize_translations

        blocks = [{"id": "p4-b3", "text": "a"}, {"id": "p4-b4", "text": "b"}]
        data = _normalize_translations({"p4-b3": "甲", "p4-b4": "乙"})
        assert _collect(data, blocks) == {"p4-b3": "甲", "p4-b4": "乙"}

    def test_alternate_item_keys(self):
        from carrel.translate.translator import _collect, _normalize_translations

        blocks = [{"id": "p1-b1", "text": "a"}]
        data = _normalize_translations({"translations": [{"id": "p1-b1", "translation": "甲"}]})
        assert _collect(data, blocks) == {"p1-b1": "甲"}

    def test_glossary_bare_array_with_term_key(self):
        from carrel.translate.glossary import _normalize_terms

        terms = _normalize_terms([{"term": "GRPO", "zh": "组相对策略优化", "keep_original": True},
                                  {"term": "routing", "translation": "路由", "keep_original": False}])
        assert [(t["src"], t["zh"], t["keep_original"]) for t in terms] == [
            ("GRPO", "组相对策略优化", True), ("routing", "路由", False)]

    def test_flattened_assessment(self):
        from carrel.assess.evaluate import normalize_assessment

        a = normalize_assessment({"novelty": "n", "evidence_strength": "e", "reproducibility": "r",
                                  "read_priority": "High", "why": "w", "caveats": "一；二"})
        assert a["value"]["read_priority"] == "high" and a["caveats"] == ["一", "二"]
        assert a["relevance"]["to_library"] == 0.0
        assert normalize_assessment({"unrelated": 1}) is None


class TestExtractors:
    def test_spaced_latex_collapses_before_number_scan(self):
        assert numbers_in(r"$1 0 ^ { - 6 }$") == numbers_in(r"$10^{-6}$")

    def test_leading_dot_decimal_is_a_number(self):
        # "p = .018" used to produce no token at all, so .018 -> .18 was invisible.
        assert numbers_in("p = .018") == [".018"]
        assert numbers_in("p = .018") != numbers_in("p = .18")

    def test_fullwidth_parens_are_citations(self):
        assert cites_in("（Gao et al., 2026）") == cites_in("(Gao et al., 2026)")

    def test_tables_tolerate_extra_repeats_but_not_dropped_values(self):
        # Merged cells repeat a value on the target side: layout, not content.
        src = "| a | 10 |\n| b | 10 |\n| c | 20 |"
        assert hard(src, "| 甲 | 10 |\n| 乙 | 10 |\n| 丙 | 20 |\n| 合计 | 20 |", block_type="table") == []
        # A dropped row whose value also appears elsewhere is still a loss.
        assert "numbers" in hard(src, "| 甲 | 10 |\n| 丙 | 20 |", block_type="table")
        # and a changed value in a table is still an error
        assert "numbers" in hard(src, "| 甲 | 10 |\n| 乙 | 10 |\n| 丙 | 21 |", block_type="table")

    def test_clean_source_and_qa_agree_on_hyphen_breaks(self):
        # The prompt side and the QA side must normalise the same way, or the
        # model is shown "ResNet-50", translates it faithfully, and is then
        # told it dropped a "50" that only the raw source ever had.
        from carrel.translate.qa import numbers_in
        from carrel.translate.translator import clean_source

        src = "We evaluate on ResNet- 50 and GPT- 4."
        assert numbers_in(clean_source(src)) == numbers_in(src)

    def test_tidy_math_keeps_row_separators_and_text_spaces(self):
        from carrel.translate.translator import clean_source

        cases = r"$\begin{cases} 1 , & x > 0 \\ 0 , & x \le 0 \end{cases}$"
        out = clean_source(cases)
        assert r"\\" in out and r"\le" in out
        assert "0,&x" not in out.replace(" ", "") or r"\\" in out  # the row break survived
        txt = clean_source(r"$\text{ for all } x \in S$")
        assert r"\text{for all}" in txt and r"\in S" in txt
        # and the original purpose still holds
        assert clean_source(r"$1 0 ^ { - 6 }$") == r"$10^{-6}$"

    def test_repair_acceptance_is_per_block(self):
        # One block improved, another regressed: keep the first, roll back the
        # second. Module-level counting let the regression through.
        from carrel.translate.glossary import Glossary
        from carrel.translate.qa import QAReport, check_module
        from carrel.translate.translator import Translator

        class Backend:
            def raw_structured(self, **kw):
                from carrel.model import ModelReply
                data = {"translations": [
                    {"block_id": "b1", "text": "准确率 4.2%。"},          # fixes b1
                    {"block_id": "b2", "text": "成本降低 3.0 倍。"},      # breaks b2 (3.2x -> 3.0)
                ]}
                return ModelReply(text="", data=data, tokens_in=1, tokens_out=1)

        blocks = [{"id": "b1", "text": "Accuracy is 4.2%.", "type": "text"},
                  {"id": "b2", "text": "Cost drops by 3.2x.", "type": "text"}]
        bad = {"b1": "准确率 4.5%。", "b2": "成本降低 3.2 倍，另有 7 项。"}
        tr = Translator(Backend())
        report = check_module(source_blocks=blocks, translations=bad, skip_types=set())
        merged, run, after = tr._repair(kind="k", all_blocks=blocks, failing={"b1", "b2"},
                                        translations=bad, report=report,
                                        glossary=Glossary(), context="")
        assert merged["b1"] == "准确率 4.2%。"          # kept: hard issues 1 -> 0
        assert merged["b2"] == bad["b2"]                 # rolled back: not strictly better
        assert run["accepted"] is True and run["blocks_kept"] == 1 and run["blocks_changed"] == 2

    def test_repair_prompt_only_sees_hard_checks(self):
        # The repair routine is told only about hard failures; soft flags stay
        # as review markers. Guarded here via the public constant.
        assert HARD_CHECKS == frozenset({"numbers", "citations", "relations",
                                         "missing", "extra", "empty"})
