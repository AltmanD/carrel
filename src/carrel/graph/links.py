"""Display links for relation rows, shared by the HTML pages and the graph API.

Both views must agree on where a row leads, so the rule lives in one place: the
local library first, then arXiv/DOI, then the provider's own URL, then the
OpenAlex work page. A bare title search is a last resort and is always labelled
as a search so it is never mistaken for the work itself.
"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit
from carrel.titles import display_title

# Anything that looks like a provider identifier rather than a human title.
_IDENTIFIER_LIKE = re.compile(r"^(?:openalex:|sciverse:|arxiv:|doi:|W\d+$)", re.I)
_PUBLIC_SCHEMES = ("https", "http")


def public_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in _PUBLIC_SCHEMES or not parsed.netloc:
        return ""
    return text


def display_label(record: dict[str, Any], local: dict[str, Any] | None) -> str:
    label = display_title(local) if local else None
    label = str(label or record.get("dst_title") or "").strip()
    if _IDENTIFIER_LIKE.match(label):
        label = ""
    return label or "文献标题暂缺"


def relation_link(
    record: dict[str, Any],
    *,
    local_lookup: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """Resolve one relation row into ``{label, url, internal, search_link, link_kind}``.

    ``link_kind`` is one of ``local`` (a page in this library), ``source`` (a
    publisher/arXiv/DOI/OpenAlex link), ``search`` (a title query) or
    ``unavailable`` (no usable destination).
    """
    row = dict(record)
    dst = str(record.get("dst_id") or "")
    work = work_id(dst)
    local = local_lookup(dst) if dst else None
    row["label"] = display_label(record, local)
    row["internal"] = False
    row["search_link"] = False

    if local:
        row["internal"] = True
        row["url"] = f"/papers/{dst}"
        row["link_kind"] = "local"
        return row

    if dst.startswith("arxiv:"):
        row["url"] = f"https://arxiv.org/abs/{dst[6:]}"
    elif dst.startswith("doi:"):
        row["url"] = f"https://doi.org/{dst[4:]}"
    elif record.get("dst_arxiv"):
        row["url"] = f"https://arxiv.org/abs/{record['dst_arxiv']}"
    elif record.get("dst_doi"):
        row["url"] = f"https://doi.org/{record['dst_doi']}"
    elif public_url(record.get("dst_url")):
        row["url"] = public_url(record.get("dst_url"))
    elif work:
        row["url"] = f"https://openalex.org/{work}"
    elif row["label"] != "文献标题暂缺":
        row["url"] = "https://scholar.google.com/scholar?" + urlencode({"q": row["label"]})
        row["search_link"] = True
    else:
        # A bare provider id with no title and no page would render as a dead
        # link; say "unavailable" instead of linking to nothing meaningful.
        row["url"] = ""
        row["link_kind"] = "unavailable"
        return row

    row["link_kind"] = "search" if row["search_link"] else "source"
    return row


def work_id(dst_id: str) -> str:
    return dst_id.split(":", 1)[1] if dst_id.startswith("openalex:") else ""
