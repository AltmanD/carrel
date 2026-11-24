"""Local sync and human curation endpoints."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from carrel.sync.apply import apply_scan, preview
from carrel.sync.scanner import scan


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    root: str = Field(min_length=1, max_length=4096)


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scan_id: str
    selected: list[str] = Field(min_length=1, max_length=10000)


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = ""
    name: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=10000)


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    collection_id: str = ""
    priority: str = Field(default="", max_length=100)
    body: str = Field(default="", max_length=100000)


def register_local_sync(app, config, db, templates, guard, error):
    templates.env.globals["local_sync_enabled"] = True
    lock = threading.Lock()
    scans = {}

    @app.get("/sync", response_class=HTMLResponse)
    def sync_page(request: Request):
        return templates.TemplateResponse(request, "sync.html", {})

    @app.get("/collections", response_class=HTMLResponse)
    def collections_page(request: Request):
        return templates.TemplateResponse(request, "collections.html", {"collections": db.collections()})

    @app.post("/api/sync/scan")
    def sync_scan(payload: SyncRequest, request: Request):
        if rejected := guard(request):
            return rejected
        root = payload.root.strip().strip('"').strip()
        if not root:
            return error(400, "invalid_directory", "请输入本地论文目录")
        if not lock.acquire(blocking=False):
            return error(409, "sync_busy", "目录扫描或同步正在进行")
        try:
            result = scan(Path(root), collections_path=config.data_dir / "collections.toml")
            now = time.monotonic()
            for key, (created, _) in list(scans.items()):
                if now - created > 1800:
                    del scans[key]
            while len(scans) >= 4:
                del scans[next(iter(scans))]
            scan_id = uuid4().hex
            scans[scan_id] = (now, result)
            return {"scan_id": scan_id, **preview(result, db)}
        except (ValueError, OSError) as exc:
            return error(400, "scan_failed", str(exc))
        finally:
            lock.release()

    @app.post("/api/sync/apply")
    def sync_apply(payload: ApplyRequest, request: Request):
        if rejected := guard(request):
            return rejected
        if not lock.acquire(blocking=False):
            return error(409, "sync_busy", "目录扫描或同步正在进行")
        try:
            saved = scans.get(payload.scan_id)
            if saved is None or time.monotonic() - saved[0] > 1800:
                return error(409, "scan_expired", "预览已过期，请重新扫描目录")
            return apply_scan(saved[1], db, config.data_dir, selected=payload.selected)
        except (ValueError, OSError) as exc:
            return error(409, "sync_failed", str(exc))
        finally:
            lock.release()

    @app.get("/api/collections")
    def get_collections():
        return {"collections": db.collections()}

    @app.post("/api/collections")
    def save_collection(payload: CollectionRequest, request: Request):
        if rejected := guard(request):
            return rejected
        name = payload.name.strip()
        if not name:
            return error(422, "invalid_name", "请输入集合名称")
        cid = payload.id or "manual-" + uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        with db.transaction() as conn:
            if payload.id and not conn.execute("SELECT 1 FROM collections WHERE id=?", (cid,)).fetchone():
                return error(404, "not_found", "阅读集合不存在")
            conn.execute("INSERT INTO collections(id,name,note,created_at) VALUES(?,?,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET name=excluded.name,note=excluded.note",
                         (cid, name, payload.note.strip(), now))
        return {"id": cid, "collections": db.collections()}

    @app.delete("/api/collections/{collection_id}")
    def delete_collection(collection_id: str, request: Request):
        if rejected := guard(request):
            return rejected
        db.delete_collection(collection_id)
        return {"ok": True, "collections": db.collections()}

    @app.get("/api/papers/{paper_id:path}/notes")
    def get_notes(paper_id: str):
        if not db.get_paper(paper_id):
            return error(404, "not_found", "论文不存在")
        return {"notes": db.notes_for(paper_id), "collections": db.collections()}

    @app.post("/api/papers/{paper_id:path}/notes")
    def save_note(paper_id: str, payload: NoteRequest, request: Request):
        if rejected := guard(request):
            return rejected
        if not db.get_paper(paper_id):
            return error(404, "not_found", "论文不存在")
        if not payload.body.strip() and not payload.priority.strip():
            return error(422, "empty_note", "请填写笔记或阅读优先级")
        now = datetime.now(timezone.utc).isoformat()
        with db.transaction() as conn:
            if payload.collection_id:
                if not conn.execute("SELECT 1 FROM collections WHERE id=?", (payload.collection_id,)).fetchone():
                    return error(422, "unknown_collection", "阅读集合不存在")
                conn.execute("INSERT OR IGNORE INTO paper_collections(paper_id,collection_id) VALUES(?,?)",
                             (paper_id, payload.collection_id))
            conn.execute("INSERT INTO notes(paper_id,collection_id,priority,body,source,created_at,updated_at) "
                         "VALUES(?,?,?,?,'human',?,?) ON CONFLICT(paper_id,collection_id) DO UPDATE SET "
                         "priority=excluded.priority,body=excluded.body,source='human',updated_at=excluded.updated_at",
                         (paper_id, payload.collection_id, payload.priority.strip(), payload.body.strip(), now, now))
        return {"notes": db.notes_for(paper_id)}

    @app.delete("/api/papers/{paper_id:path}/notes")
    def delete_general_note(paper_id: str, request: Request, collection_id: str = ""):
        if rejected := guard(request):
            return rejected
        db.execute("DELETE FROM notes WHERE paper_id=? AND collection_id=?", (paper_id, collection_id))
        return {"notes": db.notes_for(paper_id)}

    @app.delete("/api/papers/{paper_id:path}/notes/{collection_id}")
    def delete_note(paper_id: str, collection_id: str, request: Request):
        return delete_general_note(paper_id, request, collection_id)
