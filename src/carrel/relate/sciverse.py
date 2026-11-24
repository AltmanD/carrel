"""Sciverse client.

Sciverse exposes five endpoints and **no** citation-relation endpoint, so this
module only handles similarity search and metadata lookup. Citing/cited-by come
from OpenAlex.
"""

from __future__ import annotations

import time
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import httpx
from carrel.runtime.retry import check_response, with_retry, RetryPolicy
from carrel.runtime.limits import ResourceUnavailable

from carrel.models import Relation


class SciverseError(RuntimeError):
    code = "sciverse_error"


class SciverseClient:
    name = "sciverse"

    def __init__(self, *, token: str, base_url: str = "https://api.sciverse.space",
                 timeout_s: int = 60, runtime=None) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.policy = runtime.policy if runtime else RetryPolicy()
        self.resource = runtime.pool("sciverse") if runtime else None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise SciverseError("SCIVERSE_API_TOKEN is not configured")
        def attempt():
            resp = httpx.post(f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=self.timeout_s)
            check_response(resp)
            return resp.json()
        try:
            return with_retry(attempt, self.policy, resource=self.resource)
        except (httpx.HTTPError, ResourceUnavailable, ValueError) as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', '')
            raise SciverseError(f'Sciverse HTTP {status}: {type(exc).__name__}') from exc

    def catalog(self) -> dict[str, Any]:
        """Self-describing field list; used to probe capability at startup."""
        return self._post("/meta-catalog", {})

    def similar(self, *, query: str, limit: int = 10) -> list[Relation]:
        if not query.strip():
            return []
        data = self._post("/agentic-search", {"query": query, "top_k": limit})
        hits = data.get("hits") or []
        rows: list[Relation] = []
        for rank, hit in enumerate(hits[:limit]):
            doc_id = str(hit.get("doc_id") or "")
            rows.append(Relation(
                src_id="",
                dst_id=f"sciverse:{doc_id}" if doc_id else f"sciverse:rank{rank}",
                kind="similar",
                dst_title=str(hit.get("title") or ""),
                dst_year=hit.get("year"),
                dst_arxiv=str(hit.get("arxiv_id") or ""),
                dst_doi=str(hit.get("doi") or ""),
                dst_url=_public_url(hit.get("url")),
                provider=self.name,
                coverage="complete",
                rank=rank,
                score=float(hit.get("score") or 0.0),
                fetched_at=_now(),
            ))
        self.hydrate(rows)
        return rows

    def hydrate(self, rows: list[Relation]) -> int:
        """Resolve search hits to publisher metadata without guessing a paper URL."""
        updated = 0
        for row in rows:
            if not row.dst_title or row.dst_doi or row.dst_arxiv or row.dst_url:
                continue
            try:
                data = self._post("/meta-search", {
                    "query": row.dst_title, "filters": [], "page": 1, "page_size": 5,
                })
            except SciverseError:
                break  # Keep search results useful when metadata is unavailable.
            results = data.get("results") or []
            doc_id = row.dst_id.removeprefix("sciverse:")
            work = next((item for item in results if item.get("doc_id") == doc_id), None)
            if work is None:
                work = next((item for item in results
                             if _title_key(item.get("title", "")) == _title_key(row.dst_title)), None)
            if work is None:
                continue
            row.dst_title = str(work.get("title") or row.dst_title)
            doi = str(work.get("doi") or "").strip()
            row.dst_doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
            year = work.get("publication_published_year")
            if isinstance(year, (int, float)) and 1000 <= year <= 9999:
                row.dst_year = int(year)
            urls = [_public_url(location.get("url")) for location in work.get("locations") or []]
            access_urls = work.get("access_oa_url") or []
            urls.extend(_public_url(url) for url in ([access_urls] if isinstance(access_urls, str) else access_urls))
            row.dst_url = next((url for url in urls if url), "")
            for value in [row.dst_doi, *urls]:
                match = re.search(r"(?:arxiv\.org/(?:abs|pdf|src)/|arxiv\.)(\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?", value, re.I)
                if match:
                    row.dst_arxiv = match.group(1)
                    break
            if row.dst_doi or row.dst_arxiv or row.dst_url:
                updated += 1
        return updated


def _title_key(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(title)).casefold().split())


def _public_url(value: Any) -> str:
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("https", "http") and parsed.netloc else ""
    except ValueError:
        return ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
