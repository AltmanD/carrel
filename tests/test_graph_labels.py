"""Method names are a display projection; original titles remain evidence."""
import pytest

from carrel.graph.labels import paper_label
from carrel.graph.service import GraphService
from carrel.store.db import Database
from carrel.store.paths import atomic_write_json, paper_dir


def test_named_contribution_without_glossary():
    title = 'Mastering Atari, Go, Chess and Shogi by Planning with a Learned Model'
    assert paper_label(title, summary={'contributions': [
        '提出 MuZero 算法，将 tree-based search 与 learned model 相结合。',
    ]}) == 'MuZero'
    assert paper_label(title, summary={'contributions': ['与 MuZero 算法进行比较。']}) != 'MuZero'
    assert paper_label(title, summary={'contributions': ['提出 alignment 方法。']}) != 'alignment'
    assert paper_label(title, summary={'contributions': [
        '已有工作提出 AlphaZero 算法，依赖环境规则。', '本文提出 MuZero 算法。',
    ]}) == 'MuZero'
    assert paper_label(title, summary={'contributions': [
        '提出 alignment 方法。', '提出 MuZero 算法。',
    ]}) == 'MuZero'


@pytest.mark.parametrize("title,expected", [
    ("CPU-ARC: Agentic Reasoning Caching for Reasoning Reuse", "CPU-ARC"),
    ("Towards Foundation Decision Models: Drex", "Drex"),
    ("P^2-MOPD: Multi-Teacher Distillation", "P^2-MOPD"),
    ("ARE DENSE REWARDS WORTH THEIR TOKENS IN GRPO?", "DENSE REWARDS"),
])
def test_named_title_and_unnamed_study(title, expected):
    assert paper_label(title) == expected


def test_baseline_core_algorithm_does_not_replace_papers_own_method():
    glossary = {"terms": [
        {"src": "GRPO", "note": "论文的核心优化算法缩写"},
        {"src": "EmoReward", "note": "本文提出的方法名，避免与子模块混淆"},
    ]}
    assert paper_label("EmoReward: Reward Calibration", glossary=glossary) == "EmoReward"


def test_method_from_attributed_terminology_or_contribution():
    glossary = {"terms": [
        {"src": "OtherMethod", "note": "基线方法名"},
        {"src": "LoCo", "note": "本文提出的方法名"},
    ]}
    assert paper_label("Think Less, Locally: Local Preference Learning", glossary=glossary) == "LoCo"
    glossary = {"terms": [{"src": "AGE / Audit-Guided Evolution", "keep_original": True, "note": "框架名"}]}
    assert paper_label("Beneath the Surface: Agent Evolution", glossary=glossary,
                       summary={"contributions": ["把算子修订组合为 AGE。"]}) == "AGE"


def test_generic_core_concept_is_not_a_named_method():
    glossary = {"terms": [
        {"src": "alignment", "zh": "对齐", "note": "本文核心概念"},
        {"src": "Platonic Representation Hypothesis (PRH)", "zh": "柏拉图表示假说"},
    ]}
    assert paper_label("Where Does Alignment Emerge? The Platonic Representation Hypothesis",
                       glossary=glossary) == "柏拉图表示假说"
    assert paper_label("New Study", glossary={"terms": None}) == "New Study"


def test_graph_label_refresh_preserves_title_and_source(tmp_path):
    db = Database(tmp_path / "app.db")
    pid = "arxiv:2401.00001"
    title = "Words Caught in the Heart: Simulating Expression"
    try:
        db.upsert_paper({"id": pid, "title": title, "title_zh": "被困在心里的话", "status": "ready"})
        service = GraphService(db, tmp_path)
        atomic_write_json(paper_dir(tmp_path, pid) / 'classification.json',
                          {'origin': 'human', 'discipline': 'Computer Science'})
        before = service.library_graph()
        glossary_path = paper_dir(tmp_path, pid) / "glossary.json"
        atomic_write_json(glossary_path, {"terms": [{"src": "Pandora", "note": "本文提出的方法名"}]})
        source = glossary_path.read_bytes()
        after = service.library_graph()
        paper = next(n for n in after.nodes if n.role == "paper")
        assert paper.label == "Pandora"
        assert paper.title == "被困在心里的话"
        assert paper.meta["title_original"] == title
        assert before.revision != after.revision
        assert glossary_path.read_bytes() == source
        assert db.get_paper(pid)["title"] == title
    finally:
        db.close()
