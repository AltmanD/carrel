"""Transactional application of a server-owned, read-only scan result."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from carrel.ids import paper_id_for, sha256_file
from carrel.store.db import Database
from carrel.store.paths import atomic_write_json, atomic_write_text
from carrel.sync.scanner import ScanResult


def _existing(conn, paper: dict):
    row = conn.execute("SELECT * FROM papers WHERE pdf_sha256=? ORDER BY created_at, id LIMIT 1",
                       (paper["pdf_sha256"],)).fetchone()
    if row:
        return row, row["id"]
    pid = paper["id"]
    row = conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    if row and row["pdf_sha256"] and row["pdf_sha256"] != paper["pdf_sha256"]:
        pid = paper_id_for(sha256=paper["pdf_sha256"])
        row = conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    return row, pid


def preview(result: ScanResult, db: Database) -> dict:
    data = result.to_dict()
    for paper in data["papers"]:
        rows = db.query("SELECT id, title, status FROM papers WHERE pdf_sha256=? OR (id=? AND pdf_sha256='')",
                        (paper["pdf_sha256"], paper["id"]))
        paper["existing"] = dict(rows[0]) if rows else None
    data["stats"]["existing"] = sum(p["existing"] is not None for p in data["papers"])
    return data


def apply_scan(result: ScanResult, db: Database, data_dir: Path, *, selected: list[str] | None = None) -> dict:
    selected_set = set(selected) if selected is not None else {p["pdf_sha256"] for p in result.papers}
    if not selected_set or not selected_set.issubset({p["pdf_sha256"] for p in result.papers}):
        raise ValueError("请选择预览中的论文；预览已变化时请重新扫描")
    papers = [p for p in result.papers if p["pdf_sha256"] in selected_set]
    # Recheck bytes, not just size/mtime: the preview may have been open for a while.
    for paper in papers:
        source = Path(paper["source_path"])
        if not source.is_file() or sha256_file(source) != paper["pdf_sha256"]:
            raise ValueError(f"PDF 已移动或发生变化，请重新扫描：{source}")
    now = datetime.now(timezone.utc).isoformat()
    backup = data_dir / "_backup" / f"sync-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    db.backup(backup / "app.db")
    atomic_write_json(backup / "collections-notes.json", {
        "collections": db.collections(), "notes": [dict(r) for r in db.query("SELECT * FROM notes")]})
    rules_path = data_dir / "collections.toml"
    if rules_path.is_file():
        atomic_write_text(backup / "collections.toml", rules_path.read_text(encoding="utf-8"))
    if (data_dir / "sync_report.json").is_file():
        atomic_write_text(backup / "sync_report.json", (data_dir / "sync_report.json").read_text(encoding="utf-8"))
    report = {"root": result.root, "created_at": now, "backup": str(backup),
              "selected": len(papers), "added": 0, "existing": 0, "collections_added": 0,
              "notes_added": 0, "notes_updated": 0, "human_notes_preserved": 0,
              "duplicates": sum(len(p["sources"]) - 1 for p in papers),
              "skipped": result.skipped, "warnings": list(result.warnings),
              "unmatched_notes": result.unmatched_notes, "paper_ids": [], "papers": []}
    collection_ids = {s["collection_id"] for p in papers for s in p["sources"]}
    mapping = {}
    with db.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for collection in result.collections:
            if collection["id"] not in collection_ids:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO collections(id,name,source_path,kind,note,sort,created_at) VALUES(?,?,?,?,?,?,?)",
                               (*(collection[k] for k in ("id", "name", "source_path", "kind", "note", "sort")), now))
            report["collections_added"] += cur.rowcount
        for paper in papers:
            existing, pid = _existing(conn, paper)
            mapping[paper["pdf_sha256"]] = pid
            report["paper_ids"].append(pid)
            report["papers"].append({"id": pid, "pdf_sha256": paper["pdf_sha256"], "sources": paper["sources"]})
            if existing:
                report["existing"] += 1
                # Fill gaps only. Processed fields, curated titles and status belong to the existing row.
                updates = {k: paper[k] for k in ("title", "authors", "year", "abstract", "source_url", "pdf_sha256")
                           if not existing[k] or existing[k] == "[]"}
                if not existing["source_path"] or (existing["status"] == "indexed" and not Path(existing["source_path"]).is_file()):
                    updates["source_path"] = paper["source_path"]
                if "authors" in updates:
                    updates["authors"] = json.dumps(updates["authors"], ensure_ascii=False)
                if updates:
                    conn.execute("UPDATE papers SET " + ",".join(f"{k}=?" for k in updates) + " WHERE id=?", (*updates.values(), pid))
            else:
                conn.execute("INSERT INTO papers(id,title,authors,year,abstract,source_url,source_kind,source_path,pdf_sha256,status,created_at,updated_at) "
                             "VALUES(?,?,?,?,?,?,'local_path',?,?,'indexed',?,?)",
                             (pid, paper["title"], json.dumps(paper["authors"], ensure_ascii=False), paper["year"],
                              paper["abstract"], paper["source_url"], paper["source_path"], paper["pdf_sha256"], now, now))
                from carrel.profile import PRESETS
                conn.execute('UPDATE papers SET profile=? WHERE id=?', (json.dumps(PRESETS['indexed'].to_dict()), pid))
                report["added"] += 1
            for source in paper["sources"]:
                conn.execute("INSERT OR IGNORE INTO paper_collections(paper_id,collection_id,rank) VALUES(?,?,?)",
                             (pid, source["collection_id"], source["rank"]))
        for note in result.notes:
            pid = mapping.get(note["pdf_sha256"])
            if not pid:
                continue
            old = conn.execute("SELECT * FROM notes WHERE paper_id=? AND collection_id=?", (pid, note["collection_id"])).fetchone()
            if old and old["source"] == "human":
                report["human_notes_preserved"] += 1
                continue
            values = (note["priority"], note["body"], note["source_ref"])
            if old and all(old[k] == note[k] for k in ("priority", "body", "source_ref")):
                continue
            conn.execute("INSERT INTO notes(paper_id,collection_id,priority,body,source_ref,source,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,'imported',?,?) ON CONFLICT(paper_id,collection_id) DO UPDATE SET "
                         "priority=excluded.priority,body=excluded.body,source_ref=excluded.source_ref,updated_at=excluded.updated_at",
                         (pid, note["collection_id"], *values, now, now))
            report["notes_updated" if old else "notes_added"] += 1
    # A report/config write failure must not misrepresent a committed database sync as a rollback.
    try:
        if not rules_path.exists():
            atomic_write_text(rules_path, result.rules)
        atomic_write_json(data_dir / "sync_report.json", report)
    except OSError as exc:
        report["warnings"].append({"path": str(data_dir), "reason": f"同步已完成，但报告或映射表保存失败：{exc}"})
    return report
