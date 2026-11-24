"""Stable identifiers, alias resolution and de-duplication for graph nodes.

Identifiers come from several providers (local imports, OpenAlex, Sciverse) and
each spells the same work differently. Everything here is pure: it never touches
the database, so the rules can be tested directly.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

_ARXIV_PREFIX = re.compile(r"^\s*(?:arxiv:|https?://arxiv\.org/(?:abs|pdf)/)", re.I)
_ARXIV_VERSION = re.compile(r"v\d+$")
_DOI_PREFIX = re.compile(r"^\s*(?:doi:|https?://(?:dx\.)?doi\.org/)", re.I)
_OPENALEX = re.compile(r"^\s*openalex:(W\d+)\s*$", re.I)
_BARE_WORK = re.compile(r"^\s*(W\d+)\s*$", re.I)


def normalize_arxiv(value: Any) -> str:
    """Lowercase an arXiv id and drop the version suffix (``2402.03300v3``)."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = _ARXIV_PREFIX.sub("", text)
    text = text.split("/")[-1]
    text = _ARXIV_VERSION.sub("", text)
    return text.strip().lower()


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _DOI_PREFIX.sub("", text).strip().lower()


def openalex_id(value: Any) -> str:
    text = str(value or "").strip()
    match = _OPENALEX.match(text) or _BARE_WORK.match(text)
    if match:
        return match.group(1).upper()
    return ""


def normalize_title(value: Any) -> str:
    """Comparison-only title form.

    NFKC, casefold and whitespace folding — but punctuation and math symbols are
    kept, because two records that differ only in ``$x^2$`` are not the same work.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.casefold()
    return " ".join(text.split())


def category_key(path: dict[str, Any] | Iterable[str]) -> str:
    """Serialise a category path without gluing parts with ``/``.

    Custom names may contain ``/``, ``|``, quotes or spaces, so each part is
    length-prefixed: ``["计算机科学", "机器学习"]`` → ``5:计算机科学4:机器学习``.
    """
    parts = ([str(path.get(k) or "").strip() for k in ("discipline", "field", "subfield")]
             if isinstance(path, dict) else [str(p or "").strip() for p in path])
    while parts and not parts[-1]:
        parts.pop()
    return "".join(f"{len(part)}:{part}" for part in parts)


def category_id(path: dict[str, Any] | Iterable[str]) -> str:
    return "category:" + category_key(path)


def tag_id(tag: str) -> str:
    return "tag:" + tag


def paper_node_id(paper_id: str) -> str:
    return "paper:" + paper_id


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\-._]+", "-", value).strip("-").lower()
    return cleaned or hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def work_node_id(kind: str, value: str) -> str:
    if kind == "arxiv":
        return "work:arxiv:" + _slug(normalize_arxiv(value))
    if kind == "doi":
        return "work:doi:" + _slug(normalize_doi(value))
    if kind == "openalex":
        return "work:openalex:" + (openalex_id(value) or _slug(value))
    return "work:" + kind + ":" + _slug(value)


@dataclass(slots=True)
class PaperAlias:
    """One library paper and every identifier it can be reached by."""

    paper_id: str
    title: str = ""
    year: int | None = None
    arxiv: str = ""
    doi: str = ""
    url: str = ""
    title_key: str = ""
    link_kind: str = "local"

    def __post_init__(self) -> None:
        self.title_key = normalize_title(self.title)


@dataclass(slots=True)
class AliasIndex:
    """Map any identifier of a known library paper back to that paper."""

    by_id: dict[str, str] = field(default_factory=dict)
    by_arxiv: dict[str, str] = field(default_factory=dict)
    by_doi: dict[str, str] = field(default_factory=dict)
    by_title: dict[str, list[str]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    years: dict[str, int] = field(default_factory=dict)
    aliases: dict[str, PaperAlias] = field(default_factory=dict)

    @classmethod
    def from_papers(cls, papers: Iterable[dict[str, Any]]) -> "AliasIndex":
        index = cls()
        for paper in papers:
            paper_id = str(paper.get("id") or "")
            if not paper_id:
                continue
            alias = PaperAlias(
                paper_id=paper_id,
                title=str(paper.get("title") or ""),
                year=paper.get("year"),
                arxiv=normalize_arxiv(paper.get("arxiv") or _arxiv_from_id(paper_id)),
                doi=normalize_doi(paper.get("doi") or _doi_from_id(paper_id)),
                url=f"/papers/{paper_id}" if not paper_id.startswith("/") else paper_id,
            )
            index.add(alias)
        return index

    def add(self, alias: PaperAlias) -> None:
        self.aliases[alias.paper_id] = alias
        self.by_id[alias.paper_id] = alias.paper_id
        for key, value in (("by_arxiv", alias.arxiv), ("by_doi", alias.doi)):
            if value:
                getattr(self, key).setdefault(value, alias.paper_id)
        self.titles[alias.paper_id] = alias.title_key
        if alias.year:
            self.years[alias.paper_id] = int(alias.year)
        if alias.title_key:
            self.by_title.setdefault(alias.title_key, []).append(alias.paper_id)

    def match_arxiv(self, value: Any) -> str:
        return self.by_arxiv.get(normalize_arxiv(value), "")

    def match_doi(self, value: Any) -> str:
        return self.by_doi.get(normalize_doi(value), "")

    def match_title(self, title: Any, year: Any = None) -> str:
        """Title fallback for records with no usable identifier.

        A shared title is not enough: the year must not contradict, and a title
        shared by several library papers is ambiguous and matches nothing.
        """
        candidates = self.by_title.get(normalize_title(title), [])
        if len(candidates) != 1:
            return ""
        paper_id = candidates[0]
        try:
            given = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            given = None
        known = self.years.get(paper_id)
        if given and known and given != known:
            return ""
        return paper_id

    def local_paper(self, record: dict[str, Any]) -> str:
        """Resolve one relation record to a library paper id, or ``""``."""
        dst = str(record.get("dst_id") or "")
        for candidate in (dst, str(record.get("dst_url") or "")):
            if candidate in self.by_id:
                return self.by_id[candidate]
        for value in (record.get("dst_arxiv"), dst if dst.startswith("arxiv:") else ""):
            found = self.match_arxiv(value)
            if found:
                return found
        for value in (record.get("dst_doi"), dst if dst.startswith("doi:") else ""):
            found = self.match_doi(value)
            if found:
                return found
        has_identifier = bool(record.get("dst_arxiv") or record.get("dst_doi")
                              or openalex_id(dst)
                              or re.match(r"^(?:arxiv:|doi:|openalex:|sciverse:)", dst, re.I))
        if has_identifier:
            return ""
        return self.match_title(record.get("dst_title"), record.get("dst_year"))


def _arxiv_from_id(paper_id: str) -> str:
    return paper_id.split(":", 1)[1] if paper_id.startswith("arxiv:") else ""


def _doi_from_id(paper_id: str) -> str:
    return paper_id.split(":", 1)[1] if paper_id.startswith("doi:") else ""


@dataclass(slots=True)
class ExternalKey:
    """Canonical key for a work that is not in the library."""

    key: str
    kind: str
    title: str = ""
    year: int | None = None
    arxiv: str = ""
    doi: str = ""
    url: str = ""


_IDENTIFIER_ORDER = ("arxiv", "doi", "openalex", "sciverse")


def record_identifiers(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Every reliable identifier a record carries, in canonical order."""
    found: list[tuple[str, str]] = []
    arxiv = normalize_arxiv(record.get("dst_arxiv"))
    if arxiv:
        found.append(("arxiv", arxiv))
    doi = normalize_doi(record.get("dst_doi"))
    if doi:
        found.append(("doi", doi))
    dst = str(record.get("dst_id") or "")
    work = openalex_id(dst)
    if work:
        found.append(("openalex", work))
    if dst.startswith("sciverse:"):
        found.append(("sciverse", dst.split(":", 1)[1]))
    return found


def fallback_key(record: dict[str, Any]) -> str:
    """Identity for a record that carries no identifier at all.

    Using source paper, relation kind and rank keeps two anonymous rows from
    collapsing into one node — shrinking them together would invent a work.
    """
    src = str(record.get("src_id") or "")
    kind = str(record.get("kind") or "")
    rank = record.get("rank")
    rank_key = str(rank) if rank not in (None, "") else ""
    return f"anon:{src}:{kind}:{rank_key}"


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


@dataclass(slots=True)
class ExternalRegistry:
    """Merge relation rows that describe the same external work.

    A record with an arXiv id and a record with only the DOI of the same paper
    must land on one node, so merging runs over the whole identifier set rather
    than picking the first non-empty field.
    """

    uf: _UnionFind = field(default_factory=_UnionFind)
    members: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    row_anchors: list[str] = field(default_factory=list)

    def add(self, record: dict[str, Any]) -> None:
        identifiers = record_identifiers(record)
        if identifiers:
            anchor = self._key(identifiers[0])
            self.uf.find(anchor)
            for kind, value in identifiers[1:]:
                self.uf.union(anchor, self._key((kind, value)))
        else:
            anchor = fallback_key(record)
            self.uf.find(anchor)
        self.members.setdefault(anchor, []).append(record)
        self.row_anchors.append(anchor)

    @staticmethod
    def _key(identifier: tuple[str, str]) -> str:
        kind, value = identifier
        return f"{kind}:{value}"

    def resolve(self) -> dict[str, ExternalKey]:
        """Collapse the union-find into one :class:`ExternalKey` per work."""
        grouped, _ = self.resolve_grouped()
        return {key: entry for key, (entry, _) in grouped.items()}

    def resolve_grouped(self) -> tuple[dict[str, tuple[ExternalKey, list[dict[str, Any]]]],
                                       list[str]]:
        """Like :meth:`resolve`, but also report which key each added row got.

        The row list follows insertion order, so callers can read the answer for
        the i-th row they added — the same record can carry two identifiers that
        another record links together.
        """
        groups: dict[str, list[str]] = {}
        for anchor in list(self.uf.parent):
            groups.setdefault(self.uf.find(anchor), []).append(anchor)
        out: dict[str, tuple[ExternalKey, list[dict[str, Any]]]] = {}
        root_for_anchor: dict[str, str] = {}
        for root, keys in groups.items():
            records = [record for key in keys for record in self.members.get(key, [])]
            if not records:
                continue
            identifiers = {kind: value for rec in records for kind, value in record_identifiers(rec)}
            chosen_kind = next((k for k in _IDENTIFIER_ORDER if k in identifiers), "")
            if chosen_kind:
                key = work_node_id(chosen_kind, identifiers[chosen_kind])
            else:
                key = "work:anon:" + _slug(root)
            best = _best_record(records)
            out[key] = (ExternalKey(
                key=key,
                kind=chosen_kind or "anon",
                title=str(best.get("dst_title") or ""),
                year=_int_or_none(best.get("dst_year")),
                arxiv=identifiers.get("arxiv", ""),
                doi=identifiers.get("doi", ""),
                url=str(best.get("dst_url") or ""),
            ), records)
            for anchor in keys:
                root_for_anchor[anchor] = key
        return out, [root_for_anchor.get(anchor, "") for anchor in self.row_anchors]


def _best_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the most informative row: a title beats a bare id, then length."""
    return max(records, key=lambda r: (bool(str(r.get("dst_title") or "").strip()),
                                       len(str(r.get("dst_title") or ""))))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
