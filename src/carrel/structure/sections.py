"""Section detection.

MinerU gives title blocks with a heading level. We map headings onto the fixed
``SectionKind`` set; anything unrecognised becomes ``unknown`` rather than a new
kind, so downstream navigation stays predictable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from carrel.models import SECTION_KINDS, Block, Section

_HEADING_NUM = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[A-Z]\.)\s+(.{2,120})$", re.I
)
# Captures "3", "3.2", "IV." so a subsection can inherit its parent's kind.
_PARENT_NUM = re.compile(r"^\s*(\d+)(\.\d+)*\.?(?:\s|$)")

# Ordered: earlier patterns win. Matched against a normalised lower-case title.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("abstract", ("abstract", "summary")),
    ("related_work", ("related work", "relatedworks", "literature review",
                      "prior work", "background and related")),
    ("background", ("background", "preliminaries", "notation", "problem setup",
                    "problem formulation", "preliminary")),
    ("introduction", ("introduction", "extended introduction", "motivation", "overview",
                      "the problem in one paragraph")),
    ("method", ("method", "methods", "methodology", "approach", "proposed method",
                "our approach", "model", "architecture", "framework", "design",
                "algorithm", "system design", "technical approach")),
    ("experiments", ("experiment", "experiments", "experimental setup",
                     "experimental setup and results", "setup", "evaluation setup",
                     "implementation details", "training details")),
    ("results", ("result", "results", "evaluation", "empirical evaluation",
                 "ablation", "ablation study", "analysis", "findings")),
    ("discussion", ("discussion", "discussions")),
    ("limitations", ("limitation", "limitations", "threats to validity")),
    ("conclusion", ("conclusion", "conclusions", "concluding remarks",
                    "future work", "conclusion and future work")),
    ("acknowledgments", ("acknowledg", "acknowledgements", "acknowledgments")),
    ("references", ("reference", "references", "bibliography")),
    ("appendix", ("appendix", "appendices", "supplementary", "supplemental")),
]


def normalise_title(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip()
    # Strip Arabic ("3.", "3.2"), Roman ("II.", "IV.") and letter ("A.") prefixes.
    text = re.sub(r"^(?:\d+(?:\.\d+)*|[IVXLC]+|[A-Z])\.?\s+", "", text)
    return text.strip().lower().rstrip(":.")


def classify_heading(raw: str) -> str:
    """Return a :data:`carrel.models.SectionKind` for a heading string."""
    key = normalise_title(raw)
    if not key:
        return "unknown"
    for kind, needles in _RULES:
        for needle in needles:
            if key == needle or key.startswith(needle):
                return kind
    return "unknown"


def _parent_number(raw: str) -> str:
    """Return the leading section number (``"3"`` or ``"3.2"``), else ``""``."""
    m = _PARENT_NUM.match(raw or "")
    if not m:
        return ""
    return (m.group(1) or "") + (m.group(2) or "")


def _looks_like_heading(block: Block) -> bool:
    if block.type == "title":
        return True
    text = block.text.strip()
    if not text or len(text) > 140 or text.endswith("."):
        return False
    if block.level is not None:
        return True
    # Unlabelled short lines that carry a numbering prefix.
    return bool(_HEADING_NUM.match(text)) and len(text.split()) <= 14


@dataclass(slots=True)
class SectionBuilder:
    """Accumulates blocks into sections.

    A heading whose kind matches the previous section's is folded into it: that is
    what makes "3 Method" plus subsections "3.1" / "3.2" one readable unit, and
    what re-joins a section broken by a page break. Subsection headings are kept
    in place (they are real content); only a verbatim repeated running header is
    dropped.
    """

    def build(self, blocks: list[Block]) -> tuple[list[Section], list[str]]:
        sections: list[Section] = []
        current: Section | None = None
        current_blocks: list[Block] = []
        preamble: list[Block] = []
        dropped: set[str] = set()
        # Numbered parent -> kind, so "3.2 The Director" inherits "method".
        parent_kinds: dict[str, str] = {}

        def flush() -> None:
            nonlocal current, current_blocks
            if current is None:
                return
            if not current_blocks:
                current = None
                return
            current.block_ids = [b.id for b in current_blocks]
            current.page_end = max(b.page for b in current_blocks)

            mergeable = current.kind not in ("unknown", "appendix")
            last = sections[-1] if sections else None
            if mergeable and last is not None and last.kind == current.kind:
                ids = list(current.block_ids)
                # A running header repeats the section title on the next page;
                # drop only that, and only when the text is identical.
                if (current_blocks[0].type == "title"
                        and current.title.strip().lower() == last.title.strip().lower()):
                    dropped.add(ids[0])
                    ids = ids[1:]
                last.block_ids.extend(ids)
                last.page_end = max(last.page_end, current.page_end)
            else:
                sections.append(current)
            current = None
            current_blocks = []

        for block in blocks:
            if _looks_like_heading(block) and block.type == "title":
                title = block.text.strip()
                kind = classify_heading(title)
                numbered = _HEADING_NUM.match(title)

                parent = _parent_number(title)
                # A numbered subsection belongs to its chapter: "3.2 The Director"
                # is part of Method even when its own words match another rule
                # (e.g. "3.1 Problem Formulation" reads like Background).
                if parent and "." in parent:
                    top = parent.split(".")[0]
                    inherited = parent_kinds.get(top)
                    if inherited:
                        kind = inherited
                elif kind == "unknown" and parent:
                    inherited = parent_kinds.get(parent)
                    if inherited:
                        kind = inherited

                if kind != "unknown" or numbered:
                    flush()
                    current = Section(
                        id=f"sec-{len(sections) + 1}",
                        kind=kind,  # type: ignore[arg-type]
                        title=title,
                        page_start=block.page,
                        page_end=block.page,
                        block_ids=[],
                    )
                    if parent and "." not in parent and kind != "unknown":
                        parent_kinds[parent] = kind
                    # The heading itself belongs to the section it opens, so it
                    # stays addressable for translation and anchoring.
                    current_blocks = [block]
                    continue
            if current is None:
                preamble.append(block)
            else:
                current_blocks.append(block)
        flush()

        if preamble:
            head = Section(
                id="sec-0",
                kind="unknown",
                title="Front matter",
                page_start=preamble[0].page,
                page_end=preamble[-1].page,
                block_ids=[b.id for b in preamble],
            )
            sections.insert(0, head)

        assigned = {bid for s in sections for bid in s.block_ids}
        unassigned = [b.id for b in blocks if b.id not in assigned and b.id not in dropped]
        # Backfill stragglers onto the nearest preceding section.
        for bid in unassigned:
            for section in reversed(sections):
                section.block_ids.append(bid)
                break
        return sections, unassigned


def build_sections(blocks: list[Block]) -> tuple[list[Section], list[str]]:
    return SectionBuilder().build(blocks)


def sections_payload(sections: list[Section], unassigned: list[str]) -> dict:
    return {
        "sections": [s.to_dict() for s in sections],
        "unassigned_blocks": unassigned,
        "kinds": list(SECTION_KINDS),
    }


def section_blocks(sections: list[Section], by_id: dict[str, Block],
                   section: Section) -> list[Block]:
    return [by_id[bid] for bid in section.block_ids if bid in by_id]
