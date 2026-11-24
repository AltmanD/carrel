"""Read-only import previews and source-title lookup."""

from __future__ import annotations

import asyncio

import httpx

from carrel.ids import extract_inputs
from carrel.source_titles import _clean_title, fetch_title
from carrel.store.db import Database

MAX_IMPORT_ITEMS = 50
MAX_IMPORT_TEXT = 100_000


async def preview_imports(text: str, db: Database) -> dict:
    if len(text) > MAX_IMPORT_TEXT:
        raise ValueError("输入内容过长，请分批导入。")
    inputs, duplicates = extract_inputs(text)
    if not inputs:
        raise ValueError("未找到可导入的论文链接或本地 PDF 路径。")
    if len(inputs) > MAX_IMPORT_ITEMS:
        raise ValueError(f"一次最多确认 {MAX_IMPORT_ITEMS} 篇论文，请分批导入。")
    rows = []
    for item in inputs:
        existing = db.get_paper(item.paper_id)
        title = _clean_title((existing or {}).get("title"))
        rows.append({
            "input": item.source_url or item.locator,
            "paper_id": item.paper_id,
            "kind": item.kind,
            "title": title,
            "title_status": "found" if title else "unavailable" if item.arxiv_id else "pending_parse",
            "existing": bool(existing),
        })

    pending = [(item, row) for item, row in zip(inputs, rows) if item.arxiv_id and not row["title"]]
    if pending:
        semaphore = asyncio.Semaphore(4)
        async with httpx.AsyncClient(timeout=5, follow_redirects=True,
                                     headers={"User-Agent": "Carrel/0.1 (paper import preview)"}) as client:
            async def hydrate(item, row):
                async with semaphore:
                    title = await fetch_title(item, client)
                    if title:
                        row.update(title=title, title_status="found")

            # A slow source must not keep the confirmation form waiting forever.
            try:
                async with asyncio.timeout(12):
                    await asyncio.gather(*(hydrate(item, row) for item, row in pending))
            except TimeoutError:
                pass
    return {"papers": rows, "duplicates": duplicates}
