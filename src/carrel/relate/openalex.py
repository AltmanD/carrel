"""OpenAlex client: structured citation edges.

``referenced_works`` gives everything the paper cites and ``filter=cites:`` gives
its citing papers, both as stable IDs. That removes any need to regex-parse a
PDF's reference list, which fails silently across APA/IEEE/ACM formatting.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from carrel.runtime.retry import check_response, with_retry, RetryPolicy
from carrel.runtime.limits import ResourceUnavailable

from carrel.models import Relation


class OpenAlexError(RuntimeError):
    code = "openalex_error"


def _norm_work_id(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if value.startswith("https://openalex.org/"):
        value = value.split("/")[-1]
    return value.upper() if value.startswith("w") else value


def _arxiv_from_work(work: dict[str, Any]) -> str:
    for loc in work.get("locations") or []:
        landing = (loc.get("landing_page_url") or "") + " " + (loc.get("pdf_url") or "")
        if "arxiv.org" in landing:
            for token in landing.replace("/pdf/", "/abs/").split("/"):
                pass
            import re

            m = re.search(r"(\d{4}\.\d{4,5})", landing)
            if m:
                return m.group(1)
    ids = work.get("ids") or {}
    doi = str(ids.get("doi") or ids.get("DOI") or "")
    m = None
    import re

    m = re.search(r"arxiv\.(\d{4}\.\d{4,5})", doi, re.I)
    return m.group(1) if m else ""


class OpenAlexClient:
    name = "openalex"

    def __init__(self, *, base_url: str = "https://api.openalex.org",
                 mailto: str = "", api_key: str = "", timeout_s: int = 60, runtime=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.mailto = mailto
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.policy = runtime.policy if runtime else RetryPolicy()
        self.resource = runtime.pool("openalex") if runtime else None

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.api_key:
            # A personal key carries its own daily budget, immune to the shared
            # NAT-IP pool being drained by everyone else on the network.
            params = {**params, "api_key": self.api_key}
        if self.mailto:
            params = {**params, "mailto": self.mailto}
        def attempt():
            resp = httpx.get(f"{self.base_url}{path}", params=params, timeout=self.timeout_s)
            check_response(resp)
            return resp.json()
        try:
            return with_retry(attempt, self.policy, resource=self.resource)
        except (httpx.HTTPError, ResourceUnavailable, ValueError) as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', '')
            message = 'rate_limited: OpenAlex 请求过于频繁，请检查 API key。' if status == 429 else f'OpenAlex HTTP {status}: {type(exc).__name__}'
            raise OpenAlexError(message) from exc

    # ------------------------------------------------------------- lookup
    def resolve(self, *, arxiv_id: str = "", doi: str = "", title: str = "") -> dict[str, Any] | None:
        params: dict[str, Any] = {"per-page": 5}
        if doi:
            params["filter"] = f"doi:{doi.lower()}"
        elif arxiv_id:
            params["filter"] = f"locations.landing_page_url:arxiv.org/abs/{arxiv_id}"
        elif title:
            params["search"] = title
        else:
            return None
        data = self._get("/works", params)
        results = data.get("results") or []
        if not results and title:
            data = self._get("/works", {"search": title, "per-page": 5})
            results = data.get("results") or []
        return results[0] if results else None

    # ------------------------------------------------------------ relations
    def cited_works(self, work: dict[str, Any], *, limit: int = 80) -> list[Relation]:
        ids = [_norm_work_id(u) for u in (work.get("referenced_works") or [])][:limit]
        rows: list[Relation] = []
        for rank, wid in enumerate(ids):
            rows.append(Relation(
                src_id="", dst_id=f"openalex:{wid}", kind="cites",
                provider=self.name, coverage="partial", rank=rank, fetched_at=_now(),
            ))
        return rows

    def citing_works(self, work: dict[str, Any], *, limit: int = 20) -> list[Relation]:
        wid = _norm_work_id(work.get("id", ""))
        if not wid:
            return []
        data = self._get("/works", {
            "filter": f"cites:{wid}",
            "sort": "cited_by_count:desc",
            "per-page": max(1, min(limit, 50)),
        })
        rows: list[Relation] = []
        for rank, item in enumerate((data.get("results") or [])[:limit]):
            rows.append(_to_relation(item, kind="cited_by", provider=self.name, rank=rank))
        return rows

    def search_title(self, title: str, *, limit: int = 10) -> list[Relation]:
        if not title:
            return []
        data = self._get("/works", {"search": title, "per-page": min(limit, 25)})
        return [
            _to_relation(item, kind="similar", provider=self.name, rank=i, score=None)
            for i, item in enumerate((data.get("results") or [])[:limit])
        ]

    def hydrate(self, openalex_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch known references across both corpora, then retry missing IDs."""
        if not openalex_ids:
            return []
        ids = list(dict.fromkeys(_norm_work_id(i.split(":", 1)[1])
                                 for i in openalex_ids if i.startswith("openalex:")))
        ids = [wid for wid in ids if re.fullmatch(r"W\d+", wid)]
        out: list[dict[str, Any]] = []
        for chunk in _chunks(ids, 20):
            filter_value = "|".join(chunk)
            try:
                # Referenced IDs can belong to XPAC, which default searches omit.
                data = self._get("/works", {"filter": f"openalex:{filter_value}",
                                            "per-page": len(chunk), "corpus": "all"})
            except OpenAlexError:
                continue
            found = {_norm_work_id(work.get("id", "")): work
                     for work in data.get("results") or []
                     if work.get("title") or work.get("display_name")}
            for wid in chunk:
                work = found.get(wid)
                if work is None:
                    try:
                        work = self._get(f"/works/{wid}", {})
                    except OpenAlexError:
                        continue
                if _norm_work_id(work.get("id", "")) == wid:
                    out.append(work)
        return out


def _to_relation(work: dict[str, Any], *, kind: str, provider: str, rank: int,
                 score: float | None = None) -> Relation:
    wid = _norm_work_id(work.get("id", ""))
    doi = str((work.get("ids") or {}).get("doi") or work.get("doi") or "")
    return Relation(
        src_id="",
        dst_id=f"openalex:{wid}" if wid else f"doi:{doi}",
        kind=kind,  # type: ignore[arg-type]
        dst_title=str(work.get("title") or work.get("display_name") or ""),
        dst_year=work.get("publication_year"),
        dst_arxiv=_arxiv_from_work(work),
        dst_doi=doi.replace("https://doi.org/", ""),
        dst_url=str((work.get("primary_location") or {}).get("landing_page_url") or ""),
        provider=provider,
        coverage="complete",
        rank=rank,
        score=score,
        fetched_at=_now(),
    )


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
