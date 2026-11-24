"""Structure package."""

from carrel.structure.extract import (
    Extractor,
    MODULE_SCHEMA,
    normalize_module_payload,
    render_modules,
)
from carrel.structure.preview import PREVIEW_SCHEMA, build_preview
from carrel.structure.sections import (
    build_sections,
    classify_heading,
    normalise_title,
    section_blocks,
    sections_payload,
)
from carrel.structure.summarize import Summarizer, summary_markdown

__all__ = [
    "Extractor",
    "MODULE_SCHEMA",
    "Summarizer",
    "build_sections",
    "classify_heading",
    "normalise_title",
    "render_modules",
    "section_blocks",
    "sections_payload",
    "summary_markdown",
]
