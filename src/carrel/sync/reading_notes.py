"""Extract reading intent from Markdown tables, sections and reading lists."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from carrel.sync.scanner import ARXIV

_REASON = re.compile(r"为什么|重点|定位|理由|必.*写|决定|备注|关注|用途|建议|目的")
_PRIORITY = re.compile(r"优先级|顺序|编号")


def _inline(token) -> tuple[str, list[str]]:
    text, refs = [], []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            text.append(child.content)
            if child.type == "code_inline":
                refs.append(child.content)
        elif child.type == "link_open":
            refs.append(child.attrGet("href") or "")
        elif child.type in ("softbreak", "hardbreak"):
            text.append("\n")
    return "".join(text), refs


def extract_notes(root: Path, markdowns: list[Path], papers: list[dict], warnings: list) -> tuple[list[dict], list[dict]]:
    parser = MarkdownIt("commonmark").enable("table")
    by_path, by_aid = {}, {}
    for paper in papers:
        for source in paper["sources"]:
            by_path[str(Path(source["path"]).resolve()).casefold()] = (paper, source)
        if paper["arxiv_id"]:
            by_aid.setdefault(paper["arxiv_id"], []).append(paper)
    notes, unmatched = {}, []

    def match(md: Path, text: str, refs: list[str]):
        found = {}
        for ref in refs:
            decoded = unquote(ref)
            if urlsplit(decoded).scheme in ("http", "https"):
                continue
            try:
                path = (md.parent / decoded.split("#", 1)[0].replace("\\", "/")).resolve()
            except (ValueError, OSError):
                continue
            pair = by_path.get(str(path).casefold())
            if pair is None and path.suffix.lower() == ".md" and path.is_relative_to(root):
                adjacent = [ps for ps in by_path.values() if Path(ps[1]["path"]).parent == path.parent]
                if len({p["pdf_sha256"] for p, _ in adjacent}) == 1:
                    pair = adjacent[0]
            if pair:
                found[(pair[0]["pdf_sha256"], pair[1]["collection_id"])] = pair
        if found:
            return list(found.values())
        aids = {m[1] for m in ARXIV.finditer(" ".join([text, *refs]))}
        for aid in aids:
            candidates = by_aid.get(aid, [])
            local = [(p, s) for p in candidates for s in p["sources"]
                     if Path(s["path"]).is_relative_to(md.parent)]
            # A bare arXiv reference must not guess between different PDF versions.
            if len({p["pdf_sha256"] for p, _ in local}) > 1:
                continue
            if local:
                pair = max(local, key=lambda ps: len(Path(ps[1]["path"]).parent.parts))
            elif len(candidates) == 1:
                pair = (candidates[0], candidates[0]["sources"][0])
            else:
                continue
            found[(pair[0]["pdf_sha256"], pair[1]["collection_id"])] = pair
        return list(found.values())

    def add(md: Path, line: int, text: str, refs: list[str], priority: str, body: str):
        source_ref = f"{md}#L{line}"
        matches = match(md, text, refs)
        if not matches:
            unmatched.append({"source_ref": source_ref, "text": text, "priority": priority, "body": body})
            return
        priority = priority.strip()
        if p := re.search(r"\bP[0-9]\b", priority, re.I):
            priority = p[0].upper()
        for paper, source in matches:
            key = (paper["pdf_sha256"], source["collection_id"])
            note = notes.setdefault(key, {"pdf_sha256": key[0], "paper_id": paper["id"],
                "collection_id": key[1], "priority": priority[:100], "body": "", "source_ref": ""})
            if not note["priority"]:
                note["priority"] = priority[:100]
            if body and body not in note["body"]:
                note["body"] = (note["body"] + "\n\n" + body).strip()
            if source_ref not in note["source_ref"].splitlines():
                note["source_ref"] = (note["source_ref"] + "\n" + source_ref).strip()

    for md in sorted(markdowns):
        try:
            if md.stat().st_size > 2_000_000:
                warnings.append({"path": str(md), "reason": "阅读说明超过 2 MB，未解析"})
                continue
            source = md.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            warnings.append({"path": str(md), "reason": str(exc)})
            continue
        lines = source.splitlines()
        tokens = parser.parse(source)
        headers, row, line, in_table = [], [], 0, False
        for token in tokens:
            if token.type == "table_open":
                headers, in_table = [], True
            elif token.type == "table_close":
                in_table = False
            elif token.type == "tr_open":
                row = []
                line = (token.map or [0])[0] + 1
            elif token.type == "inline" and in_table:
                row.append(_inline(token))
            elif token.type == "tr_close":
                if not headers:
                    headers = [text for text, _ in row]
                    continue
                reason_cols = [i for i, h in enumerate(headers) if _REASON.search(h)]
                if not reason_cols:
                    continue
                priority_col = next((i for i, h in enumerate(headers) if _PRIORITY.search(h)), None)
                priority = row[priority_col][0] if priority_col is not None and priority_col < len(row) else ""
                body = "\n".join(f"{headers[i]}：{row[i][0]}" for i in reason_cols if i < len(row) and row[i][0])
                add(md, line, " | ".join(t for t, _ in row), [ref for _, refs in row for ref in refs], priority, body)
        context = ""
        for index, token in enumerate(tokens):
            if token.type == "heading_open" and index + 1 < len(tokens):
                heading, refs = _inline(tokens[index + 1])
                if re.search(r"阶段|第\s*[一二三四五六七八九十\d]+\s*组", heading):
                    context = heading
                if not match(md, heading, refs):
                    continue
                end = next((t.map[0] for t in tokens[index + 1:]
                            if t.type == "heading_open" and t.map), len(lines))
                start = token.map[0]
                body = "\n".join(lines[start + 1:end]).strip()
                if _REASON.search(body):
                    add(md, start + 1, heading, refs, context, body)
            elif token.type == "list_item_open" and token.map:
                # Reading lists often use a code span for the local PDF path.
                start, end = token.map
                inline = next((t for t in tokens[index + 1:] if t.type == "inline"), None)
                if inline is None or inline.map[0] != start:
                    continue
                text, refs = _inline(inline)
                if not context or not refs:
                    continue
                body = "\n".join(lines[start + 1:end]).strip()
                add(md, start + 1, text, refs, context, body or text)
    return list(notes.values()), unmatched
