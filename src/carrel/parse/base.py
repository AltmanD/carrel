"""Parser interface and the stable document model."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Protocol

from carrel.models import Block, DocModel

# MinerU leaves inline HTML in body text: author marks ("<sup>1,2</sup>"),
# entities ("&amp;") and stray tags. Kept verbatim they render as mojibake in
# the reading view, so they are cleaned once, here, at ingestion.
_TAG = re.compile(r"</?(sup|sub|i|b|em|strong|span|a)\b[^>]*>|<br\s*/?>", re.I)
_ANY_TAG = re.compile(r"<[^>]{1,120}>")


def clean_text(raw: str) -> str:
    """Strip HTML markup MinerU leaves inside PDF text, keeping the content."""
    if not raw or "<" not in raw and "&" not in raw:
        return raw
    text = _TAG.sub("", raw)          # keep inner text of inline tags
    text = _ANY_TAG.sub(" ", text)    # anything else becomes a space
    text = html.unescape(text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


class ParseError(RuntimeError):
    """Base class for parse failures. ``code`` is stable for the UI."""

    code = "parse_error"


class ParseUnavailable(ParseError):
    code = "parse_unavailable"


class ParseRemoteFailed(ParseError):
    code = "parse_remote_failed"


class ParseLowQuality(ParseError):
    code = "parse_low_quality"


class Parser(Protocol):
    name: str

    def parse(self, pdf_path, *, out_dir, progress=None) -> DocModel: ...


@dataclass(slots=True)
class ParseResult:
    doc: DocModel
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"doc": self.doc.to_dict(), "warnings": self.warnings}


#: MinerU item types we drop outright: running headers and page numbers are
#: layout furniture, not content, and would pollute translation and extraction.
DROP_TYPES = frozenset({"header", "footer", "page_number", "aside_text"})

#: MinerU emits headings as ``type: "text"`` with a ``text_level`` >= 1; it never
#: emits ``type: "title"``. Verified against a real vlm-tier response.
TITLE_TYPES = frozenset({"title"})

#: A body block that begins with a standalone section word followed by a
#: capital letter is a fused heading ("Abstract We study ...") — MinerU merges
#: the heading into the paragraph on some layouts. Splitting it here restores
#: the section boundary the splitter needs.
_FUSED_HEADING = re.compile(
    r"^(Abstract|ABSTRACT|Introduction|INTRODUCTION|Conclusion|CONCLUSION"
    r"|References|REFERENCES)\s+(?=[A-Z“\"(\[])"
)


def blocks_from_content_list(content: list[dict[str, Any]]) -> list[Block]:
    """Convert MinerU ``content_list.json`` into :class:`Block` records.

    Verified shape (vlm tier): items carry ``type``, ``page_idx`` (0-based) and
    either ``text`` (+ optional ``text_level``), ``table_body``, ``content``
    (image/chart), or caption/footnote lists. Headings are ``type="text"`` with
    ``text_level`` set, so they are promoted to title blocks here — without this
    every section title would be invisible to the section splitter.
    """
    blocks: list[Block] = []
    counters: dict[int, int] = {}
    for item in content or []:
        if not isinstance(item, dict):
            continue
        raw_type = str(item.get("type") or "text")
        if raw_type in DROP_TYPES:
            continue

        page_idx = int(item.get("page_idx") or 0)
        page = page_idx + 1

        level_raw = item.get("text_level")
        level = int(level_raw) if isinstance(level_raw, (int, float)) else None

        if raw_type in TITLE_TYPES or (raw_type == "text" and level):
            btype = "title"
        elif raw_type == "text":
            btype = "text"
        elif raw_type == "ref_text":
            btype = "text"
        elif raw_type == "table":
            btype = "table"
        elif raw_type in ("image", "chart", "figure"):
            btype = "figure"
        elif raw_type in ("equation", "interline_equation"):
            btype = "formula"
        elif raw_type in ("table_caption", "image_caption", "chart_caption",
                          "table_footnote", "image_footnote", "chart_footnote"):
            btype = "caption"
        elif raw_type in ("list", "list_item"):
            btype = "list"
        else:
            btype = "other"

        text = clean_text(_block_text(item))
        if not text.strip() and btype != "figure":
            continue

        # Split a fused heading ("Abstract We study ...") into a title block
        # plus the remaining body so the section splitter sees the boundary.
        if btype == "text" and (m := _FUSED_HEADING.match(text)):
            heading = m.group(1)
            body = text[m.end():].lstrip()
            counters[page] = counters.get(page, 0) + 1
            blocks.append(Block(
                id=f"p{page}-b{counters[page]}",
                page=page, type="title", text=heading,
                bbox=list(item["bbox"]) if isinstance(item.get("bbox"), (list, tuple)) else None,
                level=level,
            ))
            if body:
                counters[page] = counters.get(page, 0) + 1
                blocks.append(Block(
                    id=f"p{page}-b{counters[page]}",
                    page=page, type="text", text=body,
                    bbox=list(item["bbox"]) if isinstance(item.get("bbox"), (list, tuple)) else None,
                    level=None,
                ))
            continue

        counters[page] = counters.get(page, 0) + 1
        blocks.append(
            Block(
                id=f"p{page}-b{counters[page]}",
                page=page,
                type=btype,  # type: ignore[arg-type]
                text=text,
                bbox=list(item["bbox"]) if isinstance(item.get("bbox"), (list, tuple)) else None,
                level=level,
            )
        )
    return blocks


def _caption_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, list) and value:
            joined = " ".join(str(v).strip() for v in value if str(v).strip())
            if joined:
                return joined
        elif isinstance(value, str) and value.strip():
            return value
    return ""


def _block_text(item: dict[str, Any]) -> str:
    raw_type = str(item.get("type") or "")

    # Tables: keep the markup body, and fold the caption in so the reader sees
    # what the table is about.
    if raw_type == "table":
        body = ""
        for key in ("table_body", "content", "text"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                body = value
                break
        caption = _caption_text(item, "table_caption", "caption")
        if caption and body:
            return f"{caption}\n\n{body}"
        if body:
            return body
        return caption or "[table image]"

    # Figures / charts: the caption is the only textual evidence available.
    if raw_type in ("image", "chart", "figure"):
        caption = _caption_text(item, "chart_caption", "image_caption", "caption")
        return caption or f"[{'chart' if raw_type == 'chart' else 'figure'}]"

    for key in ("text", "latex", "content", "table_body"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value

    # Lists may arrive as a list of strings under list_items.
    items = item.get("list_items")
    if isinstance(items, list) and items:
        joined = " ".join(str(i).strip() for i in items if str(i).strip())
        if joined:
            return joined

    return _caption_text(item, "table_caption", "image_caption", "chart_caption")


def page_count(blocks: list[Block]) -> int:
    return max((b.page for b in blocks), default=0)
