"""Shared pytest fixtures: temp data dir, fake parser, fake model backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from carrel.config import AppConfig
from carrel.models import DocModel
from carrel.parse.base import blocks_from_content_list, page_count

FIXTURES = Path(__file__).parent / "fixtures"


def sample_content_list(pages: int = 3) -> list[dict[str, Any]]:
    """A small but realistic MinerU content_list payload."""
    out: list[dict[str, Any]] = []
    title = "Recursive Self-Improvement for Agent Orchestration"
    out.append({"type": "title", "text": title, "text_level": 1, "page_idx": 0})
    out.append({"type": "title", "text": "Abstract", "text_level": 1, "page_idx": 0})
    out.append({
        "type": "text",
        "text": "We propose a self-play loop that improves an orchestrator. "
                "On all 12 datasets it improves accuracy by 4.2% with p<0.05, "
                "while cutting cost by 3.2x [12].",
        "page_idx": 0,
    })
    out.append({"type": "title", "text": "1 Introduction", "text_level": 1, "page_idx": 0})
    out.append({
        "type": "text",
        "text": "Existing orchestration methods rely on hand-designed curricula. "
                "We argue that skill withdrawal drives the proposer to pick "
                "learnable tasks, and we report 18.7% gains over the baseline [3].",
        "page_idx": 1,
    })
    out.append({"type": "title", "text": "3 Method", "text_level": 1, "page_idx": 1})
    out.append({
        "type": "text",
        "text": "The Director builds an agent graph through atomic edits. "
                "Each node receives a role and each edge defines a handoff. "
                "We optimise L = -sum log p(y|x) with a temperature of 0.7.",
        "page_idx": 1,
    })
    out.append({"type": "equation", "text": "L = -sum_i log p(y_i | x_i)", "page_idx": 1})
    out.append({"type": "title", "text": "4 Experiments", "text_level": 1, "page_idx": 2})
    out.append({
        "type": "text",
        "text": "We evaluate on GAIA, SWE-bench and WebArena. The model uses 8 GPUs "
                "for 72 hours. Results improve by 6.5 points, with an ablation "
                "removing the proposer dropping to 21.3%.",
        "page_idx": 2,
    })
    out.append({"type": "table", "table_body": "| model | score |\n| base | 42.1 |\n| ours | 48.6 |",
                "page_idx": 2})
    out.append({"type": "title", "text": "5 Conclusion", "text_level": 1, "page_idx": 2})
    out.append({"type": "text", "text": "Self-play with skill withdrawal improves "
                                        "orchestration without external scaffolds.",
                "page_idx": 2})
    out.append({"type": "title", "text": "References", "text_level": 1, "page_idx": 2})
    out.append({"type": "text", "text": "[1] Smith et al. 2024. Some prior work.", "page_idx": 2})
    # Pad so quality gates pass (>=200 chars per page).
    while len(out) < 40:
        out.append({
            "type": "text",
            "text": "Additional body text that discusses the experimental setup in "
                    "enough detail to resemble a real paper page." * 2,
            "page_idx": min(pages - 1, len(out) % pages),
        })
    return out


class FakeParser:
    """Parse backend that produces a fixed document without network access."""

    name = "fake"

    def __init__(self, content: list[dict[str, Any]] | None = None,
                 raises: Exception | None = None) -> None:
        self.content = content if content is not None else sample_content_list()
        self.raises = raises
        self.calls = 0

    def parse(self, pdf_path, *, out_dir: Path, data_id: str = "", progress=None) -> DocModel:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        blocks = blocks_from_content_list(self.content)
        return DocModel(backend="fake_vlm", pages=page_count(blocks), blocks=blocks,
                        quality=0.9)


class FakeBackend:
    """Deterministic model backend keyed on the task shape of each prompt."""

    name = "fake:test"
    protocol = "fake"

    def __init__(self, *, broken_json: bool = False, fail: bool = False,
                 bad_translation: bool = False) -> None:
        self.broken_json = broken_json
        self.fail = fail
        self.bad_translation = bad_translation
        self.calls: list[dict[str, Any]] = []

    def capabilities(self):
        from carrel.model import Capabilities

        return Capabilities(strict_schema=True, json_mode=True)

    def text(self, *, system: str, user: str, max_tokens: int = 8000,
             effort: str = "", task: str = ""):
        from carrel.model import ModelReply

        return ModelReply(text="ok", data=None, tokens_in=1, tokens_out=1, task=task)

    def structured(self, *, schema: dict[str, Any], system: str, user: str,
                   max_tokens: int = 8000, effort: str = "", task: str = ""):
        from carrel.model import ModelError, ModelReply

        if self.fail:
            raise ModelError("simulated backend failure")
        self.calls.append({"system": system[:60], "user": user[:200], "task": task})

        props = schema.get("properties", {})
        if "translations" in props:
            data = self._translations(user)
        elif "title_zh" in props:
            data = {'title_zh': self._preview()['title_zh']}
        elif task == 'preview':
            data = self._preview()
        elif "categories" in props or ("discipline" in props and "tags" in props):
            data = self._classification()
        elif "one_line" in props:
            data = self._summary()
        elif "terms" in props:
            data = self._glossary()
        elif "replacements" in props:
            data = {"replacements": []}
        elif "relevance" in props:
            data = self._assessment()
        elif "motivation" in props:
            material = json.loads(user)
            data = {"motivation": "减少智能体编排对人工课程的依赖。",
                    "problem": "智能体训练", "gap": "现有方法依赖人工课程",
                    "direction": "自动化智能体编排", "keywords": ["智能体", "自博弈"],
                    "evidence_block_ids": [p["block_id"] for p in material["introduction"].get("points", [])[:1]]}
        elif "edges" in props:
            data = {"edges": []}
        elif "points" in props:
            data = self._module(user)
        else:
            data = {"ok": True}
        return ModelReply(text=json.dumps(data, ensure_ascii=False), data=data,
                          tokens_in=100, tokens_out=50, duration_ms=5,
                          task=task, model="fake", max_tokens=max_tokens,
                          stop_reason="end_turn", cost_usd=0.0001)

    def _preview(self) -> dict[str, Any]:
        return {
            "title_zh": "面向通用系统的路由研究",
            "one_line": "研究如何在异构模型间按输入选择最优候选。",
            "tldr": "问题：候选随输入变化。方法：统一路由框架。结果：多基准提升。",
            "contributions": ["统一路由理论", "全模态轨迹矩阵"],
        }

    # ---------------------------------------------------------- generators
    def _translations(self, user: str) -> dict[str, Any]:
        from carrel.translate.qa import cites_in, numbers_in, relations_in

        segments = _segments_by_block(user)
        glossary = _glossary_pairs(user)
        out = []
        for bid, src in segments.items():
            if self.bad_translation:
                text = ("这里的中文译文把 4.2% 写成了 5.0%，"
                        "并且把 p<0.05 写成了 p>0.05。")
            else:
                text = _faithful_translation(src, glossary, numbers_in,
                                             relations_in, cites_in)
            out.append({"block_id": bid, "text": text})
        return {"translations": out}

    def _summary(self) -> dict[str, Any]:
        return {
            "one_line": "用自博弈与技能撤除训练一个更会编排的智能体。",
            "tldr": "问题：编排依赖人工课程。方法：内循环自博弈。结果：12 个数据集提升 4.2%。"
                    "条件：p<0.05。局限：未开源代码。",
            "contributions": ["提出递归可学习性目标", "给出技能撤除机制"],
            "argument_chain": ["假设候选任务可学", "内循环自博弈", "撤除技能后仍可学", "结论"],
            "limitations": ["作者未报告训练数据的规模"],
            "reading_guide": {"order": ["先读 §3、§4"], "skip": ["§2"], "why": "方法与实验最相关"},
        }

    def _classification(self) -> dict[str, Any]:
        return {
            "categories": [
                {"discipline": "计算机科学", "field": "机器学习", "subfield": "强化学习"},
                {"discipline": "计算机科学", "field": "自然语言处理", "subfield": "智能体"},
            ],
            "confidence": 0.86,
            "basis": ["p1-b3"],
            "reason": "研究智能体编排与训练方法",
            "tags": [
                {"tag": "智能体编排", "evidence": "builds an agent graph", "block_id": "p2-b7"},
                {"tag": "自博弈", "evidence": "self-play loop", "block_id": "p1-b3"},
            ],
        }

    def _glossary(self) -> dict[str, Any]:
        return {"terms": [
            {"src": "orchestrator", "zh": "编排器", "note": "组织智能体协作", "keep_original": False},
            {"src": "GAIA", "zh": "GAIA", "note": "", "keep_original": True},
        ]}

    def _assessment(self) -> dict[str, Any]:
        return {
            "relevance": {"to_library": 0.8, "neighbors": [], "to_projects": []},
            "value": {"novelty": "中", "evidence_strength": "强", "reproducibility": "中",
                      "read_priority": "high", "why": "与阅读目标直接相关"},
            "caveats": ["结论依赖自建数据集"],
        }

    def _module(self, user: str) -> dict[str, Any]:
        # Use whatever block ids the prompt actually contained.
        ids = []
        for line in user.splitlines():
            line = line.strip()
            if line.startswith("[") and "]" in line:
                ids.append(line[1 : line.index("]")])
        anchor = ids[0] if ids else "p1-b1"
        return {
            "digest": "这一节说明了方法与实验设置。",
            "points": [{"text": "提出了一个方法", "block_id": anchor}],
            "definitions": [{"term": "orchestrator", "text_zh": "编排器", "block_id": anchor}],
            "formulas": [],
            "numbers": [{"what": "提升 4.2%", "block_id": anchor}],
            "open_questions": ["未说明更长的训练是否继续提升"],
        }


class FakeRelations:
    def collect(self, *, paper_id, title, arxiv_id="", doi="", abstract=""):
        from carrel.models import Relation
        from carrel.relate.link import RelationBundle

        bundle = RelationBundle()
        bundle.cites = [
            Relation(src_id=paper_id, dst_id="openalex:W111", kind="cites",
                     dst_title="A Cited Work", dst_year=2020, provider="openalex"),
        ]
        bundle.cited_by = [
            Relation(src_id=paper_id, dst_id="arxiv:2501.12948", kind="cited_by",
                     dst_title="A Citing Work", dst_year=2025, provider="openalex"),
        ]
        bundle.similar = [
            Relation(src_id=paper_id, dst_id="sciverse:doc1", kind="similar",
                     dst_title="A Similar Work", provider="sciverse", score=0.9),
        ]
        bundle.coverage = {"cites": "complete", "cited_by": "complete", "similar": "complete"}
        return bundle


class FailingRelations(FakeRelations):
    def collect(self, **kw):
        raise RuntimeError("relation provider down")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "kb"
    d.mkdir()
    return d


@pytest.fixture()
def config(data_dir: Path) -> AppConfig:
    cfg = AppConfig(data_dir=data_dir)
    cfg.mineru.token = "test-token"
    cfg.model.base_url = "http://127.0.0.1:1"
    cfg.openalex.mailto = ""
    return cfg


@pytest.fixture()
def db(config: AppConfig):
    from carrel.store.db import Database

    database = Database(config.data_dir / "app.db")
    yield database
    database.close()


@pytest.fixture()
def sample_pdf(tmp_path: Path) -> Path:
    """A tiny file that only needs to exist; the fake parser ignores contents."""
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n% test fixture\n")
    return path


@pytest.fixture()
def pipeline_factory(config: AppConfig, db, monkeypatch):
    from carrel.pipeline import Pipeline
    from carrel.profile import save_default, PRESETS
    # Existing pipeline contract tests explicitly exercise the full preset.
    save_default(config.data_dir, PRESETS['full'])

    def make(*, parser=None, backend=None, relations=None):
        pipeline = Pipeline(
            config, db,
            parser=parser or FakeParser(),
            backend=backend or FakeBackend(),
            relations=relations or FakeRelations(),
        )
        # These are pipeline contract tests, including arXiv identity checks;
        # downloading a real PDF would bypass the fake-service boundary.
        def download(url, destination):
            destination.write_bytes(b"%PDF-1.4 offline fixture\n" + url.encode("utf-8"))
        monkeypatch.setattr(pipeline, "_download", download)
        return pipeline

    return make


# --------------------------------------------------------------- test helpers

_BLOCK_LINE = __import__("re").compile(r"^\s*\[(?P<bid>[A-Za-z0-9\-]+)\](?:\s*\([^)]*\))?\s*(?P<text>.*)$")


def _segments_by_block(user: str) -> dict[str, str]:
    """Pull ``[block_id] text`` pairs out of a translation prompt.

    Handles both the first-pass format (``[id] text`` on one line) and the repair
    format (``[id]`` on its own line, then ``原文：...`` / ``当前译文：...``).
    """
    import re

    segments: dict[str, str] = {}
    current: str | None = None
    for raw in user.splitlines():
        line = raw.rstrip()
        m = _BLOCK_LINE.match(line)
        if m:
            current = m.group("bid")
            segments[current] = m.group("text").strip()
            continue
        if current and line.strip().startswith("原文："):
            segments[current] = line.split("原文：", 1)[1].strip()
        elif current and line.lstrip().startswith("|"):
            # Markdown tables span lines; retain the rows carrying their data.
            segments[current] += "\n" + line
    return segments


def _glossary_pairs(user: str) -> dict[str, str]:
    """Read ``- term → 译法`` rows out of the injected glossary table."""
    import re

    pairs: dict[str, str] = {}
    for line in user.splitlines():
        m = re.match(r"^\s*-\s+(.+?)\s+→\s+(.+?)\s*$", line)
        if not m:
            continue
        src, dst = m.group(1).strip(), m.group(2).strip()
        if dst == "保留原文不译":
            pairs[src] = ""  # must stay English
        else:
            pairs[src] = re.sub(r"（.*?）$", "", dst).strip()
    return pairs


def _faithful_translation(src: str, glossary: dict[str, str], numbers_in,
                          relations_in, cites_in) -> str:
    """Produce a translation that a competent translator would accept.

    Keeps every number, comparison operator and citation, and honours the glossary
    so the deterministic QA gate has nothing to complain about.
    """
    import re

    low = src.lower()
    used_terms = [zh for term, zh in glossary.items() if zh and term.lower() in low]
    cites = cites_in(src)
    # Digits inside a citation belong to the citation, not to the prose count;
    # strip them before collecting numbers so we don't emit "12" twice.
    prose = re.sub(r"\[[0-9,\-–\s]+\]", " ", src)
    prose = re.sub(r"\([^()]{0,80}?(?:et al\.|,)\s*\d{4}[a-z]?\)", " ", prose)
    nums = numbers_in(prose)
    rels = relations_in(src)

    if len(src) < 40:
        # Headings and short labels compress; keep any fixture digits intact.
        head = used_terms[0] if used_terms else "标题译文"
        return " ".join([head, *nums, *rels, *cites])

    frags = ["这一段的中文译文，用于验证对照阅读的排版效果。"]
    if used_terms:
        frags.append("其中使用了约定术语：" + "、".join(used_terms) + "。")
    if nums:
        frags.append("原文中的数值逐一保留在此：" + "、".join(nums) + "。")
    if rels:
        frags.append("比较条件保持不变：" + "·".join(rels) + "。")
    if cites:
        frags.append("引用编号保持不变：" + " ".join(cites) + "。")
    text = "".join(frags)
    # The fake backend must meet the length contract for every module, not just
    # the abstract. Do not let filesystem enumeration order hide fixture flaws.
    detail = "本段还说明了方法设计、实验设置和结果解释，便于读者核对论文中的论述。"
    minimum = int(len(src) * .45) + 1
    if len(text) < minimum:
        text += (detail * ((minimum - len(text)) // len(detail) + 1))[:minimum - len(text)]
    return text
