"""Contract tests: ids, model protocol layers, translation QA, taxonomy."""

from __future__ import annotations

import pytest

from carrel import ids as ids_mod
from carrel.ids import UnsupportedInput, resolve_input


class TestIds:
    def test_arxiv_abs_and_pdf_resolve_to_same_id(self):
        a = resolve_input("https://arxiv.org/abs/2501.12948")
        b = resolve_input("https://arxiv.org/pdf/2501.12948v2")
        assert a.paper_id == b.paper_id == "arxiv:2501.12948"

    def test_hf_papers_maps_to_arxiv_id(self):
        r = resolve_input("https://huggingface.co/papers/2610.22740")
        assert r.kind == "hf"
        assert r.paper_id == "arxiv:2610.22740"

    def test_arxiv_scheme(self):
        assert resolve_input("arxiv:2503.17332").paper_id == "arxiv:2503.17332"

    def test_direct_pdf_url(self):
        r = resolve_input("https://example.org/papers/a-study.pdf")
        assert r.kind == "pdf_url"
        assert r.paper_id.startswith("pdf-")

    def test_local_path(self, tmp_path):
        pdf = tmp_path / "my paper.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        r = resolve_input(str(pdf))
        assert r.kind == "local_path"
        assert r.paper_id.startswith("pdf-")

    def test_local_path_with_chinese_and_spaces(self, tmp_path):
        pdf = tmp_path / "论文 一 篇.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        assert resolve_input(str(pdf)).kind == "local_path"

    @pytest.mark.parametrize("bad", [
        "", "   ", "https://example.com/article", "/no/such/file.pdf", "not a link",
    ])
    def test_unsupported(self, bad):
        with pytest.raises(UnsupportedInput):
            resolve_input(bad)

    def test_paper_id_precedence(self):
        assert ids_mod.paper_id_for(arxiv_id="2501.12948", doi="10.1/x",
                                    sha256="a" * 64) == "arxiv:2501.12948"
        assert ids_mod.paper_id_for(doi="10.1/X").lower() == "doi:10.1/x"
        assert ids_mod.paper_id_for(sha256="b" * 64) == "pdf-" + "b" * 16

    def test_arxiv_pdf_url_normalises_version(self):
        assert ids_mod.arxiv_pdf_url("2501.12948v3") == "https://arxiv.org/pdf/2501.12948"


class TestJsonExtraction:
    def test_plain_json(self):
        from carrel.model import extract_json

        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        from carrel.model import extract_json

        assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}

    def test_prose_wrapped_json(self):
        from carrel.model import extract_json

        assert extract_json('Sure, here it is: {"a": 3} hope that helps') == {"a": 3}

    def test_invalid_raises(self):
        from carrel.model import ModelSchemaError, extract_json

        with pytest.raises(ModelSchemaError):
            extract_json("no json at all")


class TestSchemaValidation:
    SCHEMA = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "items": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            }},
        },
        "required": ["name", "items"],
        "additionalProperties": False,
    }

    def test_valid(self):
        from carrel.model import validate_schema

        assert validate_schema({"name": "x", "items": [{"id": "a"}]}, self.SCHEMA) == []

    def test_missing_required(self):
        from carrel.model import validate_schema

        errs = validate_schema({"name": "x"}, self.SCHEMA)
        assert any("items" in e for e in errs)

    def test_wrong_type(self):
        from carrel.model import validate_schema

        errs = validate_schema({"name": 5, "items": []}, self.SCHEMA)
        assert any("string" in e for e in errs)

    def test_additional_property_rejected(self):
        from carrel.model import validate_schema

        errs = validate_schema({"name": "x", "items": [], "extra": 1}, self.SCHEMA)
        assert any("extra" in e for e in errs)


class TestTranslationQA:
    def test_clean_block_passes(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="It improves accuracy by 4.2% with p<0.05 [12].",
            target="它在 p<0.05 的条件下把准确率提升 4.2% [12]。",
            translate_required=True,
        )
        assert issues == []

    def test_dropped_number_is_caught(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="It improves accuracy by 4.2% and cost by 3.2x.",
            target="它把准确率提升 4.2%，成本降低 3.0 倍。",
            translate_required=True,
        )
        assert any(i.check == "numbers" for i in issues)

    def test_tampered_number_is_caught(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="We report 18.7% gains on all 12 datasets.",
            target="我们在全部 12 个数据集上报告了 19.7% 的提升。",
            translate_required=True,
        )
        assert any(i.check == "numbers" for i in issues)

    def test_flipped_inequality_is_caught(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="The effect is significant with p<0.05.",
            target="该效应在 p>0.05 时显著。",
            translate_required=True,
        )
        assert any(i.check == "relations" for i in issues)

    def test_changed_citation_is_caught(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="Earlier work [31] shows this.",
            target="更早的工作 [13] 展示了这一点。",
            translate_required=True,
        )
        assert any(i.check == "citations" for i in issues)

    def test_untranslated_block_is_caught(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="This sentence should be translated into Chinese.",
            target="This sentence should be translated into Chinese.",
            translate_required=True,
        )
        assert any(i.check in ("residual_english", "cjk_ratio") for i in issues)

    def test_formula_block_may_stay_english(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b9",
            source="L = -sum_i log p(y_i | x_i)",
            target="L = -sum_i log p(y_i | x_i)",
            translate_required=False,
        )
        assert issues == []

    def test_missing_and_extra_blocks(self):
        from carrel.translate.qa import check_module

        report = check_module(
            source_blocks=[
                {"id": "p1-b1", "type": "text", "text": "We report 4.2% gains today."},
                {"id": "p1-b2", "type": "text", "text": "Cost falls by 3.2x overall."},
            ],
            translations={"p1-b1": "我们报告了 4.2% 的提升。", "p1-b3": "多余块"},
            skip_types=set(),
        )
        assert any(i.check == "missing" and i.block_id == "p1-b2" for i in report.issues)
        assert any(i.check == "extra" and i.block_id == "p1-b3" for i in report.issues)

    def test_glossary_term_enforced(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="The orchestrator selects tasks from a pool of candidates.",
            target="该调度器从候选池中选择任务。",
            translate_required=True,
            glossary_terms={"orchestrator": "编排器"},
        )
        assert any(i.check == "glossary" for i in issues)

    def test_kept_proper_nouns_are_not_flagged(self):
        """Transformer / SWE-bench / ReAct must survive untouched in Chinese text."""
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="We use a Transformer encoder with LSTM layers and evaluate on "
                   "the SWE-bench dataset against the ReAct baseline.",
            target="我们使用 Transformer 编码器配合 LSTM 层，在 SWE-bench 数据集上"
                   "与 ReAct 基线对比，报告通过率。",
            translate_required=True,
        )
        assert not any(i.check == "residual_english" for i in issues), issues

    def test_untranslated_prose_is_flagged(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1",
            source="This paragraph was left in English by mistake and must be caught.",
            target="This paragraph was left in English by mistake and must be caught.",
            translate_required=True,
        )
        assert any(i.check in ("residual_english", "cjk_ratio") for i in issues)

    def test_numbers_with_ranges_units_and_signs(self):
        from carrel.translate.qa import check_block

        for src, tgt in [
            ("We test on 1-5 layers.", "我们在 1-5 层上测试。"),
            ("The gap is -12.5 points.", "差距为 -12.5 分。"),
            ("Learning rate 1e-4.", "学习率 1e-4。"),
            ("12,400 samples were used.", "共使用 12,400 个样本。"),
            ("Accuracy reached 85%-92%.", "准确率达到 85%-92%。"),
            ("We use PyTorch 2.1.", "我们使用 PyTorch 2.1。"),
        ]:
            issues = check_block(block_id="p1-b1", source=src, target=tgt,
                                 translate_required=True)
            assert not any(i.check == "numbers" for i in issues), (src, issues)

    def test_short_heading_is_not_penalised_for_length(self):
        from carrel.translate.qa import check_block

        issues = check_block(
            block_id="p1-b1", source="Abstract", target="摘要",
            translate_required=True,
        )
        assert not any(i.check == "length_ratio" for i in issues)

    def test_sign_and_multiplier_are_part_of_the_number(self):
        from carrel.translate.qa import check_block, numbers_in

        assert numbers_in("-12.5 pts and 3.2x") == ["-12.5", "3.2×"]
        # A flipped sign is a different number and must be caught.
        issues = check_block(
            block_id="p1-b1", source="The gap is -12.5 points.",
            target="差距为 12.5 分。", translate_required=True,
        )
        assert any(i.check == "numbers" for i in issues)
        # A dropped multiplier must be caught.
        issues = check_block(
            block_id="p1-b1", source="Cost drops by 3.2x overall.",
            target="成本整体下降 3.2。", translate_required=True,
        )
        assert any(i.check == "numbers" for i in issues)
        # 倍 is the standard rendering of the multiplier and must pass.
        issues = check_block(
            block_id="p1-b1", source="Cost drops by 3.2x overall.",
            target="成本整体下降 3.2 倍。", translate_required=True,
        )
        assert not any(i.check == "numbers" for i in issues)
        # Hyphenated words are not numbers.
        assert "19" not in numbers_in("COVID-19 data")

    def test_cjk_ratio_helper(self):
        from carrel.translate.qa import cjk_ratio

        assert cjk_ratio("这是中文") > 0.9
        assert cjk_ratio("all english here") == 0.0


class TestTaxonomy:
    def test_default_paths_are_consistent(self):
        from carrel.classify import Taxonomy

        tax = Taxonomy.default()
        for entry in tax.paths():
            assert 'subfield' not in entry
            ok, why = tax.validate(entry["discipline"], entry["field"], None)
            assert ok, why

    def test_rejects_unknown_field(self):
        from carrel.classify import Taxonomy

        ok, why = Taxonomy.default().validate("计算机科学", "炼金术", None)
        assert not ok and "炼金术" in why

    def test_rejects_unknown_discipline(self):
        from carrel.classify import Taxonomy

        ok, _ = Taxonomy.default().validate("占星学", "", None)
        assert not ok

    def test_loads_shipped_toml(self):
        from pathlib import Path

        from carrel.classify import Taxonomy

        path = Path(__file__).parents[1] / "src" / "carrel" / "taxonomy.toml"
        tax = Taxonomy.load(path)
        assert "计算机科学" in tax.disciplines
        assert "机器学习" in tax.fields["计算机科学"]


class TestSectionMapping:
    @pytest.mark.parametrize("title,expected", [
        ("Abstract", "abstract"),
        ("1 Introduction", "introduction"),
        ("3 Method", "method"),
        ("4 Experiments", "experiments"),
        ("5 Results", "results"),
        ("Related Work", "related_work"),
        ("Limitations", "limitations"),
        ("6 Conclusion", "conclusion"),
        ("References", "references"),
        ("Appendix A", "appendix"),
        ("Something Unusual Here", "unknown"),
        # Roman-numeral numbering is common in IEEE/ACM papers.
        ("II. RELATED WORK", "related_work"),
        ("I. INTRODUCTION", "introduction"),
        ("III. METHOD", "method"),
        ("V. CONCLUSION", "conclusion"),
    ])
    def test_classify_heading(self, title, expected):
        from carrel.structure import classify_heading

        assert classify_heading(title) == expected

    def test_build_sections_respects_order(self):
        from carrel.models import Block
        from carrel.structure import build_sections

        blocks = [
            Block("p1-b1", 1, "title", "Abstract", level=1),
            Block("p1-b2", 1, "text", "An abstract body."),
            Block("p1-b3", 1, "title", "1 Introduction", level=1),
            Block("p2-b1", 2, "text", "Intro body."),
            Block("p2-b2", 2, "title", "3 Method", level=1),
            Block("p2-b3", 2, "text", "Method body."),
        ]
        sections, unassigned = build_sections(blocks)
        kinds = [s.kind for s in sections]
        assert kinds == ["abstract", "introduction", "method"]
        assert unassigned == []
        # The heading belongs to the section it opens.
        assert sections[2].block_ids == ["p2-b2", "p2-b3"]
        assert sections[1].block_ids == ["p1-b3", "p2-b1"]

    @pytest.mark.parametrize('heading', ['1 Extended Introduction', '1 The problem in one paragraph'])
    def test_nonstandard_introduction_is_selected_for_skim(self, heading):
        """Recognized introductions must reach extraction and motivation in skim."""
        from carrel.models import Block
        from carrel.profile import selected_section
        from carrel.structure import build_sections

        blocks = [
            Block("p1-b1", 1, "title", heading, level=2),
            Block("p1-b2", 1, "text", "Memory limits motivate partitioning model states."),
            Block("p2-b1", 2, "title", "2 Related Work", level=2),
            Block("p2-b2", 2, "text", "Earlier work."),
        ]
        sections, unassigned = build_sections(blocks)
        selected = [s for s in sections if selected_section(s, 'key')]
        assert [s.kind for s in selected] == ['introduction']
        assert selected[0].block_ids == ['p1-b1', 'p1-b2']
        assert not unassigned

    def test_subsections_inherit_parent_kind_without_reordering(self):
        """3.1 / 3.2 belong to Method, and reading order must be preserved."""
        from carrel.models import Block
        from carrel.structure import build_sections

        blocks = [
            Block("p1-b1", 1, "title", "3 Method", level=1),
            Block("p1-b2", 1, "text", "Top level method text."),
            Block("p1-b3", 1, "title", "3.1 Problem Formulation", level=2),
            Block("p1-b4", 1, "text", "Subsection text."),
            Block("p1-b5", 1, "title", "3.2 The Director", level=2),
            Block("p1-b6", 1, "text", "Director details."),
            Block("p1-b7", 1, "title", "4 Experiments", level=1),
            Block("p1-b8", 1, "text", "Experiment text."),
        ]
        sections, unassigned = build_sections(blocks)
        assert [s.kind for s in sections] == ["method", "experiments"]
        assert unassigned == []
        reading_order = [bid for s in sections for bid in s.block_ids]
        assert reading_order == [f"p1-b{i}" for i in range(1, 9)]

    def test_page_break_continuation_is_merged(self):
        from carrel.models import Block
        from carrel.structure import build_sections

        blocks = [
            Block("p1-b1", 1, "title", "3 Method", level=1),
            Block("p1-b2", 1, "text", "Text on page one."),
            Block("p2-b1", 2, "title", "3 Method", level=1),
            Block("p2-b2", 2, "text", "Continues on page two."),
        ]
        sections, _ = build_sections(blocks)
        assert len(sections) == 1
        assert sections[0].block_ids == ["p1-b1", "p1-b2", "p2-b2"]
