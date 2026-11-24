"""Core domain records.

These types are the contract between pipeline stages. Every stage reads and
writes plain JSON via :mod:`carrel.store.paths`, so nothing here holds file
handles or database connections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BlockType = Literal[
    "text", "title", "table", "figure", "caption", "formula", "list", "other"
]

SectionKind = Literal[
    "abstract",
    "introduction",
    "related_work",
    "background",
    "method",
    "experiments",
    "results",
    "discussion",
    "limitations",
    "conclusion",
    "appendix",
    "acknowledgments",
    "references",
    "unknown",
]

SECTION_KINDS: tuple[str, ...] = (
    "abstract",
    "introduction",
    "related_work",
    "background",
    "method",
    "experiments",
    "results",
    "discussion",
    "limitations",
    "conclusion",
    "appendix",
    "acknowledgments",
    "references",
    "unknown",
)

# Sections worth translating under the default "core" scope.
CORE_SECTIONS: tuple[str, ...] = (
    "abstract",
    "introduction",
    "method",
    "experiments",
    "results",
    "conclusion",
)


@dataclass(slots=True)
class Block:
    id: str
    page: int
    type: BlockType
    text: str
    bbox: list[float] | None = None
    level: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DocModel:
    backend: str
    pages: int
    blocks: list[Block]
    raw_ref: dict[str, Any] = field(default_factory=dict)
    quality: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "pages": self.pages,
            "blocks": [b.to_dict() for b in self.blocks],
            "raw_ref": self.raw_ref,
            "quality": self.quality,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocModel":
        """Inverse of :meth:`to_dict`, so a saved ``doc.json`` can be resumed from."""
        blocks = [
            Block(
                id=str(b.get("id", "")), page=int(b.get("page", 0) or 0),
                type=b.get("type", "text"), text=str(b.get("text", "")),
                bbox=b.get("bbox"), level=b.get("level"),
            )
            for b in data.get("blocks") or [] if isinstance(b, dict)
        ]
        return cls(
            backend=str(data.get("backend", "")),
            pages=int(data.get("pages", 0) or 0),
            blocks=blocks,
            raw_ref=dict(data.get("raw_ref") or {}),
            quality=float(data.get("quality", 0.0) or 0.0),
            warnings=list(data.get("warnings") or []),
        )

    def blocks_by_id(self) -> dict[str, Block]:
        return {b.id: b for b in self.blocks}

    def fingerprint(self) -> str:
        """Stable hash of the blocks; artefacts derived from this doc record it.

        A resume must not reuse a translation of text the document no longer
        contains, so every stage output written from a doc carries this value
        and is ignored when it does not match. Block ids are positional
        (``p3-b7``), so the hash covers id, type *and* text: a re-parse that
        keeps the layout but changes the words must invalidate the artefacts.
        """
        import hashlib

        h = hashlib.sha256()
        for b in self.blocks:
            h.update(f"{b.id}\x1f{b.type}\x1f{b.text}\n".encode("utf-8", "surrogatepass"))
        return h.hexdigest()[:16]


@dataclass(slots=True)
class Section:
    id: str
    kind: SectionKind
    title: str
    page_start: int
    page_end: int
    block_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PaperMeta:
    id: str
    title: str = ""
    title_zh: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    abstract: str = ""
    source_kind: str = ""          # arxiv | hf | pdf_url | local_path
    source_url: str = ""
    source_path: str = ""
    pdf_sha256: str = ""
    lang: str = "en"
    scope: str = "full"            # full | abstract_only
    status: str = "queued"
    profile: dict | str | None = None
    parse_backend: str = ""
    parse_quality: float = 0.0
    discipline: str = ""
    field: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModulePoint:
    text: str
    block_id: str


@dataclass(slots=True)
class ModuleExtract:
    kind: str
    title_original: str
    block_span: list[str]
    digest: str = ""
    points: list[dict[str, str]] = field(default_factory=list)
    definitions: list[dict[str, str]] = field(default_factory=list)
    formulas: list[dict[str, str]] = field(default_factory=list)
    numbers: list[dict[str, str]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Summary:
    one_line: str = ""
    # "model" = written by the summarizer; "fallback" = stitched from a module
    # digest, in which case the preview's headline is the better display choice.
    one_line_source: str = "model"
    tldr: str = ""
    contributions: list[str] = field(default_factory=list)
    section_digests: list[dict[str, str]] = field(default_factory=list)
    argument_chain: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    reading_guide: dict[str, Any] = field(default_factory=dict)
    scope: str = "full"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Tag:
    tag: str
    evidence: str
    block_id: str
    origin: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Classification:
    discipline: str = ""
    field_name: str = ""
    subfield: str | None = None
    confidence: float = 0.0
    basis: list[str] = field(default_factory=list)
    reason: str = ""
    tags: list[Tag] = field(default_factory=list)
    # A paper can belong to several specific subfields; the first entry is the
    # primary used for sorting and faceting.
    categories: list[dict[str, str]] = field(default_factory=list)
    primary_category_id: str = ""
    taxonomy_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        from carrel.classify.taxonomy import project_path
        categories = [project_path(p) for p in (self.categories or [
            {"discipline": self.discipline, "field": self.field_name, "subfield": self.subfield}
        ])]
        primary = next((p for p in categories if p["category_id"] == self.primary_category_id), categories[0])
        return {
            "discipline": primary["discipline"],
            "field": primary["field"],
            "confidence": self.confidence,
            "basis": self.basis,
            "reason": self.reason,
            "tags": [t.to_dict() for t in self.tags],
            "categories": categories,
            "primary_category_id": primary["category_id"],
            "taxonomy_version": self.taxonomy_version,
        }


@dataclass(slots=True)
class Relation:
    src_id: str
    dst_id: str
    kind: Literal["cites", "cited_by", "similar"]
    dst_title: str = ""
    dst_year: int | None = None
    dst_arxiv: str = ""
    dst_doi: str = ""
    dst_url: str = ""
    provider: str = ""
    coverage: str = "complete"
    rank: int = 0
    score: float | None = None
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModelRun:
    id: str
    paper_id: str
    task: str
    module: str = ""
    status: str = "ok"
    backend: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    schema_ok: int = 1
    qa_ok: int = 1
    retry_of: str = ""
    trace_dir: str = ""
    error_code: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
