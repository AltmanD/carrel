"""Input normalisation and stable paper identifiers.

Order of preference: arxiv id > doi > content hash. Two links to the same
arXiv paper must resolve to the same id; two different papers with similar
titles must never be merged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_ARXIV_NUMBER = r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})"
ARXIV_SCHEME = re.compile(rf"^arxiv:\s*({_ARXIV_NUMBER})(?:v\d+)?$", re.I)
ARXIV_ID = re.compile(rf"^({_ARXIV_NUMBER})(?:v\d+)?$", re.I)
_PAPER_LINK = re.compile(
    rf"(?<![a-z0-9_@./:%?=&-])(?:https?://)?"
    rf"(?:(?:(?:www|export)\.)?arxiv\.org/(?:abs|pdf|html)/"
    rf"|(?:www\.)?(?:huggingface\.co|hf\.co)/papers/)"
    rf"{_ARXIV_NUMBER}(?:v\d+)?(?:\.pdf)?"
    rf"(?![a-z0-9_/-]|\.[a-z0-9])", re.I,
)


@dataclass(slots=True)
class ResolvedInput:
    kind: str          # arxiv | hf | pdf_url | local_path
    locator: str       # what to fetch
    paper_id: str      # preview id (may be refined after hashing)
    arxiv_id: str = ""
    source_url: str = ""


class UnsupportedInput(ValueError):
    """Raised when the input matches none of the supported forms."""


def _norm_arxiv(raw: str) -> str:
    m = ARXIV_ID.match(raw.strip())
    return m.group(1) if m else raw.strip()


def resolve_input(value: str) -> ResolvedInput:
    """Classify a link or path into one of the four supported input kinds."""
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        raise UnsupportedInput("empty input")

    if m := ARXIV_SCHEME.match(text):
        aid = m.group(1)
        return ResolvedInput(
            kind="arxiv",
            locator=aid,
            paper_id=f"arxiv:{aid}",
            arxiv_id=aid,
            source_url=f"https://arxiv.org/abs/{aid}",
        )

    if re.match(r"^(?:(?:www|export)\.)?(?:arxiv\.org|huggingface\.co|hf\.co)/", text, re.I):
        text = "https://" + text

    if re.match(r"^https?://", text, re.I):
        try:
            url = urlsplit(text)
        except ValueError as exc:
            raise UnsupportedInput("invalid URL") from exc
        if any(char.isspace() for char in text) or url.username or url.password or not url.hostname:
            raise UnsupportedInput("expected a single paper link")
        host = url.hostname.lower()
        path = url.path.rstrip("/")
        kind = ""
        if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            match = re.fullmatch(rf"/(?:abs|pdf|html)/({_ARXIV_NUMBER})(?:v\d+)?(?:\.pdf)?", path, re.I)
            kind = "arxiv"
        elif host in {"huggingface.co", "www.huggingface.co", "hf.co", "www.hf.co"}:
            match = re.fullmatch(rf"/papers/({_ARXIV_NUMBER})(?:v\d+)?", path, re.I)
            kind = "hf"
        if kind:
            if not match:
                raise UnsupportedInput("not a supported paper page")
            aid = match.group(1)
            source = f"https://huggingface.co/papers/{aid}" if kind == "hf" else f"https://arxiv.org/abs/{aid}"
            return ResolvedInput(kind=kind, locator=aid, paper_id=f"arxiv:{aid}",
                                 arxiv_id=aid, source_url=source)
        if url.path.lower().endswith(".pdf"):
            return ResolvedInput(
                kind="pdf_url",
                locator=text,
                paper_id=_placeholder_id(text),
                source_url=text,
            )
        raise UnsupportedInput(
            "only arXiv links, Hugging Face paper links and direct .pdf URLs are supported"
        )

    path = Path(text.replace("file://", "", 1) if text.startswith("file://") else text)
    if path.is_file() and path.suffix.lower() == ".pdf":
        return ResolvedInput(
            kind="local_path",
            locator=str(path.resolve()),
            paper_id=_placeholder_id(str(path)),
            source_path_placeholder=str(path.resolve()),
        ) if False else ResolvedInput(
            kind="local_path",
            locator=str(path.resolve()),
            paper_id=_placeholder_id(str(path)),
            source_url="",
        )

    raise UnsupportedInput(
        "not a supported arXiv link, Hugging Face paper link, .pdf URL or local .pdf path"
    )


def extract_inputs(text: str) -> tuple[list[ResolvedInput], int]:
    """Extract supported paper links from prose, preserving first-seen order."""
    items: list[ResolvedInput] = []
    seen: set[str] = set()
    duplicates = 0
    for line in text.splitlines():
        matches = list(_PAPER_LINK.finditer(line))
        candidates = [match.group() for match in matches] if matches else [line.strip()]
        for candidate in candidates:
            try:
                item = resolve_input(candidate)
            except (UnsupportedInput, OSError, ValueError):
                continue
            key = item.paper_id if item.arxiv_id else item.locator
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            items.append(item)
    return items, duplicates


def _placeholder_id(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"pdf-{digest}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def paper_id_for(*, arxiv_id: str = "", doi: str = "", sha256: str = "") -> str:
    if arxiv_id:
        return f"arxiv:{_norm_arxiv(arxiv_id)}"
    if doi:
        return f"doi:{doi.strip().lower()}"
    if sha256:
        return f"pdf-{sha256[:16]}"
    raise ValueError("one of arxiv_id, doi or sha256 is required")


def arxiv_pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{_norm_arxiv(arxiv_id)}"
