"""Read-only PDF inventory, content identity and local metadata extraction."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from carrel.ids import paper_id_for, sha256_file
from carrel.sync.collections import collection_for, load_rules, rules_text

ARXIV = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)")
_SKIP = {"code", "repos", ".git", "__pycache__", "node_modules", ".venv", "paper-researcher"}
_METADATA = ("sources.json", "paper_index.json", "metadata.json", "papers.json")


@dataclass
class ScanResult:
    root: str
    papers: list[dict] = field(default_factory=list)
    collections: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    unmatched_notes: list[dict] = field(default_factory=list)
    pdf_count: int = 0
    rules: str = ""

    def to_dict(self) -> dict:
        result = asdict(self)
        result["stats"] = {"pdfs": self.pdf_count, "papers": len(self.papers),
                           "collections": len(self.collections), "notes": len(self.notes),
                           "duplicates": self.pdf_count - len(self.papers),
                           "duplicate_groups": sum(len(p["sources"]) > 1 for p in self.papers),
                           "skipped": len(self.skipped), "unmatched_notes": len(self.unmatched_notes)}
        return result


def _text(value) -> str:
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    return str(value).strip() if isinstance(value, (str, int)) else ""


def _records(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("papers", "items", "entries"):
            if isinstance(data.get(key), list):
                return _records(data[key])
        return [data]
    return []


def _metadata(pdf: Path, root: Path, digest: str, cache: dict, warnings: list) -> dict:
    aid = next((m[1] for part in (pdf.stem, *reversed(pdf.relative_to(root).parts[:-1]))
                if (m := ARXIV.search(part))), "")
    candidates = []
    parent = pdf.parent
    while parent.is_relative_to(root):
        for path in [*(parent / name for name in _METADATA), parent / "sources" / "metadata.json"]:
            if path not in cache:
                cache[path] = []
                if path.is_file():
                    try:
                        if path.stat().st_size > 10_000_000:
                            raise ValueError("metadata exceeds 10 MB")
                        cache[path] = _records(json.loads(path.read_text(encoding="utf-8-sig")))
                    except (ValueError, OSError) as exc:
                        warnings.append({"path": str(path), "reason": str(exc)})
            for row in cache[path]:
                meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else row
                row_aid = _text(meta.get("citation_arxiv_id") or row.get("arxiv_id") or row.get("id"))
                if m := ARXIV.search(row_aid):
                    row_aid = m[1]
                else:
                    row_aid = ""
                raw_file = _text(row.get("file") or row.get("pdf") or row.get("filename") or row.get("local_path"))
                local = path.parent / raw_file.replace("\\", "/")
                score = 0
                if raw_file and local.resolve() == pdf:
                    score = 4
                elif row.get("sha256") == digest:
                    score = 3
                elif aid and row_aid == aid:
                    score = 2
                elif len(cache[path]) == 1 and parent == pdf.parent and not raw_file and (row_aid or meta.get("title")):
                    score = 1
                if score:
                    candidates.append((score, path, row, meta, row_aid))
        if parent == root:
            break
        parent = parent.parent
    info = {"title": "", "authors": [], "year": None, "abstract": "", "arxiv_id": aid,
            "source_url": "", "metadata_sources": []}
    ordered = sorted(candidates, key=lambda c: -c[0])
    info["arxiv_id"] = next((c[4] for c in ordered if c[4]), aid)
    for _, path, row, meta, row_aid in ordered:
        info["metadata_sources"].append(str(path))
        title = _text(meta.get("citation_title") or meta.get("title") or row.get("title"))
        info["title"] = info["title"] or title[:1000]
        authors = meta.get("citation_author") or meta.get("authors") or row.get("authors") or []
        info["authors"] = info["authors"] or (authors if isinstance(authors, list) else [str(authors)])
        date = _text(meta.get("citation_date") or row.get("date") or row.get("year") or row.get("version_date") or row.get("published"))
        if m := re.search(r"\b((?:19|20)\d{2})\b", date):
            info["year"] = info["year"] or int(m[1])
        info["abstract"] = info["abstract"] or _text(meta.get("citation_abstract") or row.get("abstract") or row.get("summary"))[:50000]
        url = _text(row.get("abstract_url") or row.get("source_abs_url") or row.get("requested_url") or row.get("pdf_url"))
        if urlsplit(url).scheme in ("https", "http"):
            info["source_url"] = info["source_url"] or url
    if info["arxiv_id"]:
        info["source_url"] = f"https://arxiv.org/abs/{info['arxiv_id']}"
    if not info["title"]:
        title = re.sub(r"^\d{1,2}[_-]", "", pdf.stem)
        title = ARXIV.sub("", title).strip("_- ")
        info["title"] = title.replace("_", " ") or pdf.stem
    return info


def scan(root: Path, *, collections_path: Path | None = None) -> ScanResult:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("本地目录不存在，或当前服务无法访问该目录")
    rules = load_rules(collections_path)
    result = ScanResult(root=str(root))
    by_sha, collections, cache = {}, {}, {}
    markdowns = []
    populated = set()

    def walk_error(exc):
        result.skipped.append({"path": str(exc.filename), "reason": "无法读取目录"})

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        folder = Path(current)
        for name in sorted(dirs.copy()):
            path = folder / name
            if (name.casefold() in _SKIP or name.lower().startswith("framework_source_snapshots")
                    or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())):
                dirs.remove(name)
                result.skipped.append({"path": str(path), "reason": "源码、缓存或链接目录"})
        dirs.sort()
        for name in sorted(files):
            path = folder / name
            if path.is_symlink():
                continue
            if path.suffix.lower() == ".md" and any(k in name.lower() for k in ("readme", "index", "priority", "阅读", "guide")):
                markdowns.append(path)
            if path.suffix.lower() != ".pdf":
                continue
            if result.pdf_count >= 10000:
                raise ValueError("目录包含超过 10000 份 PDF，请选择更小的子目录")
            try:
                before = path.stat()
                digest = sha256_file(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("扫描时文件发生变化，请重新扫描")
                metadata = _metadata(path, root, digest, cache, result.warnings)
            except (OSError, ValueError) as exc:
                result.skipped.append({"path": str(path), "reason": str(exc)})
                continue
            result.pdf_count += 1
            populated.add(path.relative_to(root).parts[0])
            collection, rank = collection_for(root, path, rules)
            collections[collection["id"]] = collection
            source = {"path": str(path), "collection_id": collection["id"], "rank": rank,
                      "size": after.st_size, "mtime_ns": after.st_mtime_ns,
                      "metadata_sources": metadata.pop("metadata_sources")}
            if digest in by_sha:
                paper = by_sha[digest]
                paper["sources"].append(source)
                for key in ("authors", "year", "abstract", "arxiv_id", "source_url"):
                    if not paper.get(key) and metadata.get(key):
                        paper[key] = metadata[key]
                if source["metadata_sources"] and not paper["has_metadata"]:
                    paper["title"] = metadata["title"]
                    paper["has_metadata"] = True
            else:
                by_sha[digest] = {**metadata, "pdf_sha256": digest, "source_path": str(path),
                                  "sources": [source], "has_metadata": bool(source["metadata_sources"])}
    assigned = {}
    filenames = {}
    for paper in by_sha.values():
        pid = paper_id_for(arxiv_id=paper["arxiv_id"], sha256=paper["pdf_sha256"])
        if pid in assigned:
            result.warnings.append({"path": paper["source_path"], "reason": f"同一论文编号对应不同 PDF 内容：{pid}，保留独立条目"})
            pid = paper_id_for(sha256=paper["pdf_sha256"])
        assigned[pid] = paper["pdf_sha256"]
        paper["id"] = pid
        for source in paper["sources"]:
            name = Path(source["path"]).name.casefold()
            if name in filenames and filenames[name] != paper["pdf_sha256"]:
                result.warnings.append({"path": source["path"], "reason": "同名 PDF 内容不同，保留独立条目"})
            filenames[name] = paper["pdf_sha256"]
        result.papers.append(paper)
    skipped_paths = {s["path"] for s in result.skipped}
    for folder in root.iterdir():
        if folder.is_dir() and folder.name not in populated and str(folder) not in skipped_paths:
            result.skipped.append({"path": str(folder), "reason": "没有可同步的 PDF"})
    result.collections = sorted(collections.values(), key=lambda c: (c["name"], c["id"]))
    result.rules = rules_text(result.collections)
    from carrel.sync.reading_notes import extract_notes

    result.notes, result.unmatched_notes = extract_notes(root, markdowns, result.papers, result.warnings)
    return result
