"""Parse quality gates.

The point of these gates is to fail loudly. A resume that silently produced an
empty note is worse than a visible error, so each check maps to a stable code.
"""

from __future__ import annotations

from carrel.models import Block, DocModel
from carrel.parse.base import ParseLowQuality

MIN_CHARS_PER_PAGE = 200
MIN_TOTAL_BLOCKS = 8


def chars_per_page(blocks: list[Block], pages: int) -> float:
    if pages <= 0:
        return 0.0
    total = sum(len(b.text) for b in blocks if b.type in ("text", "list", "title"))
    return total / pages


def score(blocks: list[Block], pages: int) -> float:
    """A 0..1 heuristic: text density, block continuity and page coverage."""
    if not blocks or pages <= 0:
        return 0.0
    density = min(1.0, chars_per_page(blocks, pages) / 1200.0)

    pages_seen = len({b.page for b in blocks})
    coverage = min(1.0, pages_seen / pages)

    ids = [b.id for b in blocks]
    sequential = sum(1 for a, b in zip(ids, ids[1:]) if a != b)
    continuity = min(1.0, sequential / max(1, len(ids) - 1))

    return round(0.5 * density + 0.3 * coverage + 0.2 * continuity, 3)


def gate(doc: DocModel) -> DocModel:
    """Raise when the parse is unusable, otherwise annotate and return it."""
    cpp = chars_per_page(doc.blocks, doc.pages)
    if doc.pages <= 0:
        raise ParseLowQuality("document has zero pages")
    if len(doc.blocks) < MIN_TOTAL_BLOCKS:
        raise ParseLowQuality(
            f"only {len(doc.blocks)} content blocks extracted ({doc.pages} pages)"
        )
    if cpp < MIN_CHARS_PER_PAGE:
        raise ParseLowQuality(
            f"average {cpp:.0f} characters per page, below the {MIN_CHARS_PER_PAGE} threshold"
        )
    doc.quality = score(doc.blocks, doc.pages)
    if doc.quality < 0.25:
        doc.warnings.append("layout_suspect")
    return doc
