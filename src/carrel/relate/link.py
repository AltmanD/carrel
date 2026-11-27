"""Relation collection and local-library linking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from carrel.models import Relation
from carrel.relate.openalex import OpenAlexClient, OpenAlexError
from carrel.relate.sciverse import SciverseClient, SciverseError


@dataclass(slots=True)
class RelationBundle:
    cites: list[Relation] = field(default_factory=list)
    cited_by: list[Relation] = field(default_factory=list)
    similar: list[Relation] = field(default_factory=list)
    coverage: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cited_by_count: int | None = None

    def all(self) -> list[Relation]:
        return [*self.cites, *self.cited_by, *self.similar]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cites": [r.to_dict() for r in self.cites],
            "cited_by": [r.to_dict() for r in self.cited_by],
            "similar": [r.to_dict() for r in self.similar],
            "coverage": self.coverage,
            "errors": self.errors,
            "cited_by_count": self.cited_by_count,
        }


class RelationCollector:
    def __init__(
        self,
        *,
        openalex: OpenAlexClient | None = None,
        sciverse: SciverseClient | None = None,
        cited_by_limit: int = 20,
        similar_limit: int = 10,
        cites_limit: int = 80,
    ) -> None:
        self.openalex = openalex
        self.sciverse = sciverse
        self.cited_by_limit = cited_by_limit
        self.similar_limit = similar_limit
        self.cites_limit = cites_limit

    def collect(
        self,
        *,
        paper_id: str,
        title: str,
        arxiv_id: str = "",
        doi: str = "",
        abstract: str = "",
    ) -> RelationBundle:
        bundle = RelationBundle()

        work = None
        if self.openalex is not None:
            try:
                work = self.openalex.resolve(arxiv_id=arxiv_id, doi=doi, title=title)
            except OpenAlexError as exc:
                bundle.errors["resolve"] = str(exc)

        if work is not None and self.openalex is not None:
            count = work.get("cited_by_count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                bundle.cited_by_count = count
            try:
                bundle.cites = self.openalex.cited_works(work, limit=self.cites_limit)
                # Bare OpenAlex IDs are unreadable in the UI; hydrate titles,
                # years and DOIs right here so every import ships usable rows.
                self.hydrate_cites(bundle)
            except OpenAlexError as exc:
                bundle.errors["cites"] = str(exc)
                bundle.coverage["cites"] = "failed"
            try:
                bundle.cited_by = self.openalex.citing_works(
                    work, limit=self.cited_by_limit
                )
                bundle.coverage["cited_by"] = "complete"
            except OpenAlexError as exc:
                bundle.errors["cited_by"] = str(exc)
                bundle.coverage["cited_by"] = "failed"
        else:
            bundle.coverage["cites"] = "failed" if not bundle.errors.get("resolve") else "failed"
            bundle.coverage["cited_by"] = "failed"

        query = " ".join(x for x in (title, abstract[:300]) if x).strip()
        if self.sciverse is not None and self.sciverse.enabled and query:
            try:
                bundle.similar = self.sciverse.similar(query=query, limit=self.similar_limit)
                bundle.coverage["similar"] = "complete"
            except SciverseError as exc:
                bundle.errors["similar"] = str(exc)
                bundle.coverage["similar"] = "failed"
        else:
            bundle.coverage["similar"] = "unavailable"

        for row in bundle.all():
            row.src_id = paper_id
        return bundle

    def hydrate_cites(self, bundle: RelationBundle) -> None:
        """Fill titles, years, DOIs and arXiv ids for cited-work IDs.

        Bare OpenAlex IDs are unreadable; a title plus a working link is the
        minimum the reference list needs. Coverage reflects what hydrated.
        """
        if self.openalex is None or not bundle.cites:
            return
        ids = [r.dst_id for r in bundle.cites]
        try:
            works = self.openalex.hydrate(ids)
        except OpenAlexError:
            bundle.coverage["cites"] = "partial"
            return
        by_id = {f"openalex:{str(w.get('id', '')).split('/')[-1].upper()}": w for w in works}
        hydrated = 0
        for rel in bundle.cites:
            work = by_id.get(rel.dst_id)
            if not work:
                continue
            rel.dst_title = str(work.get("title") or work.get("display_name") or rel.dst_title)
            rel.dst_year = work.get("publication_year") or rel.dst_year
            doi = str((work.get("ids") or {}).get("doi") or work.get("doi") or "")
            rel.dst_doi = doi.replace("https://doi.org/", "") or rel.dst_doi
            rel.dst_arxiv = _arxiv_of_work(work) or rel.dst_arxiv
            rel.dst_url = str((work.get("primary_location") or {}).get("landing_page_url") or rel.dst_url)
            if rel.dst_title:
                rel.coverage = "complete"
                hydrated += 1
        bundle.coverage["cites"] = "complete" if hydrated == len(bundle.cites) else "partial"


def _arxiv_of_work(work: dict) -> str:
    """Best-effort arXiv id from an OpenAlex work record."""
    import re

    for loc in work.get("locations") or []:
        landing = str(loc.get("landing_page_url") or "")
        if "arxiv.org" in landing:
            m = re.search(r"(\d{4}\.\d{4,5})", landing)
            if m:
                return m.group(1)
    doi = str((work.get("ids") or {}).get("doi") or "")
    m = re.search(r"arxiv\.(\d{4}\.\d{4,5})", doi, re.I)
    return m.group(1) if m else ""


def link_to_library(paper_id: str, bundle: RelationBundle, known: dict[str, str]) -> dict[str, int]:
    """Mark relations whose target is already in the local library.

    ``known`` maps arxiv id and doi (lower-case) to local paper ids.
    """
    linked = {"cites": 0, "cited_by": 0, "similar": 0}
    for rel in bundle.all():
        target = ""
        if rel.dst_arxiv and rel.dst_arxiv.lower() in known:
            target = known[rel.dst_arxiv.lower()]
        elif rel.dst_doi and rel.dst_doi.lower() in known:
            target = known[rel.dst_doi.lower()]
        if target and target != paper_id:
            rel.dst_id = target
            linked[rel.kind] += 1
    return linked
