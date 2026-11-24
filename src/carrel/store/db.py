"""SQLite persistence: schema, migrations and small query helpers."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 8

_DDL = """
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  title TEXT DEFAULT '',
  title_zh TEXT DEFAULT '',
  authors TEXT DEFAULT '[]',
  year INTEGER,
  venue TEXT DEFAULT '',
  abstract TEXT DEFAULT '',
  source_kind TEXT DEFAULT '',
  source_url TEXT DEFAULT '',
  source_path TEXT DEFAULT '',
  pdf_sha256 TEXT DEFAULT '',
  lang TEXT DEFAULT 'en',
  scope TEXT DEFAULT 'full',
  status TEXT DEFAULT 'queued',
  profile TEXT DEFAULT '',
  parse_backend TEXT DEFAULT '',
  parse_quality REAL DEFAULT 0,
  discipline TEXT DEFAULT '',
  field TEXT DEFAULT '',
  created_at TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS categories (
  path TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  parent TEXT DEFAULT '',
  sort INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tags (
  paper_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  evidence TEXT DEFAULT '',
  source_block TEXT DEFAULT '',
  origin TEXT DEFAULT 'llm',
  PRIMARY KEY (paper_id, tag)
);

CREATE TABLE IF NOT EXISTS relations (
  src_id TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  dst_title TEXT DEFAULT '',
  dst_year INTEGER,
  dst_arxiv TEXT DEFAULT '',
  dst_doi TEXT DEFAULT '',
  dst_url TEXT DEFAULT '',
  provider TEXT DEFAULT '',
  coverage TEXT DEFAULT 'complete',
  rank INTEGER DEFAULT 0,
  score REAL,
  fetched_at TEXT DEFAULT '',
  PRIMARY KEY (src_id, dst_id, kind)
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT DEFAULT 'queued',
  attempts INTEGER DEFAULT 0,
  error_code TEXT DEFAULT '',
  error_detail TEXT DEFAULT '',
  started_at TEXT DEFAULT '',
  finished_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS model_runs (
  id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  task TEXT NOT NULL,
  module TEXT DEFAULT '',
  status TEXT DEFAULT 'ok',
  backend TEXT DEFAULT '',
  model TEXT DEFAULT '',
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  duration_ms INTEGER DEFAULT 0,
  schema_ok INTEGER DEFAULT 1,
  qa_ok INTEGER DEFAULT 1,
  retry_of TEXT DEFAULT '',
  trace_dir TEXT DEFAULT '',
  error_code TEXT DEFAULT '',
  created_at TEXT DEFAULT '',
  thinking_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  stop_reason TEXT DEFAULT '',
  max_tokens INTEGER DEFAULT 0,
  thinking TEXT DEFAULT '',
  truncated INTEGER DEFAULT 0,
  accepted INTEGER DEFAULT -1,
  cost_usd REAL DEFAULT 0,
  requested_model TEXT DEFAULT '',
  provider TEXT DEFAULT 'legacy'
);

CREATE TABLE IF NOT EXISTS category_proposals (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  parent TEXT DEFAULT '',
  label TEXT NOT NULL,
  paper_id TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source_path TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'dir',
  note TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_collections (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  rank INTEGER,
  PRIMARY KEY (paper_id, collection_id)
);
CREATE TABLE IF NOT EXISTS notes (
  paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  collection_id TEXT NOT NULL DEFAULT '',
  priority TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'human',
  source_ref TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (paper_id, collection_id)
);
CREATE INDEX IF NOT EXISTS idx_notes_paper ON notes(paper_id);
CREATE INDEX IF NOT EXISTS idx_pc_collection ON paper_collections(collection_id);

CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS idx_papers_discipline ON papers(discipline, field);
CREATE INDEX IF NOT EXISTS idx_papers_sha ON papers(pdf_sha256);
CREATE INDEX IF NOT EXISTS idx_jobs_paper ON jobs(paper_id);
CREATE INDEX IF NOT EXISTS idx_runs_paper ON model_runs(paper_id);
CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(src_id, kind);
"""


_RUN_V3_COLUMNS: tuple[tuple[str, str], ...] = (
    ("thinking_tokens", "INTEGER DEFAULT 0"),
    ("cache_read_tokens", "INTEGER DEFAULT 0"),
    ("cache_write_tokens", "INTEGER DEFAULT 0"),
    ("stop_reason", "TEXT DEFAULT ''"),
    ("max_tokens", "INTEGER DEFAULT 0"),
    ("thinking", "TEXT DEFAULT ''"),
    ("truncated", "INTEGER DEFAULT 0"),
    ("accepted", "INTEGER DEFAULT -1"),
    ("cost_usd", "REAL DEFAULT 0"),
    ("requested_model", "TEXT DEFAULT ''"),
)


class Database:
    """Thin SQLite wrapper.

    A single connection guarded by a lock is enough for a one-user local app and
    avoids the shared-filesystem WAL problems that showed up on network drives.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                      timeout=60)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=60000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=DELETE")
        if self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_meta'").fetchone():
            previous = self._conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if previous and int(previous[0]) < SCHEMA_VERSION:
                self.backup(self.path.parent / "_backup" / f"schema-v{previous[0]}-{datetime.now():%Y%m%d-%H%M%S-%f}.db")
        try:
            self.init_schema()
        except BaseException:
            self._conn.close()
            raise

    # ---------------------------------------------------------------- schema
    def init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_DDL)
            self._conn.execute("INSERT OR IGNORE INTO schema_meta(key,value) VALUES('graph_revision','0')")
            # Triggers include raw connection writes inside transactions and
            # other processes, and roll back with the corresponding edit.
            for table in ('papers', 'tags', 'relations', 'collections', 'paper_collections', 'categories'):
                for operation in ('INSERT', 'UPDATE', 'DELETE'):
                    self._conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS graph_{table}_{operation.lower()} "
                        f"AFTER {operation} ON {table} BEGIN "
                        "UPDATE schema_meta SET value=CAST(value AS INTEGER)+1 WHERE key='graph_revision'; END")
            for operation in ('INSERT', 'UPDATE OF status', 'DELETE'):
                suffix = operation.split()[0].lower()
                self._conn.execute(f"CREATE TRIGGER IF NOT EXISTS graph_jobs_{suffix} AFTER {operation} ON jobs BEGIN "
                                   "UPDATE schema_meta SET value=CAST(value AS INTEGER)+1 WHERE key='graph_revision'; END")
            indexes = {r['name']: dict(r) for r in self._conn.execute('PRAGMA index_list(papers)')}
            if not indexes.get('idx_papers_sha', {}).get('unique'):
                duplicates = self._conn.execute("SELECT pdf_sha256, group_concat(id) AS ids FROM papers "
                    "WHERE pdf_sha256 != '' GROUP BY pdf_sha256 HAVING COUNT(*) > 1").fetchall()
                if duplicates:
                    raise ValueError("Duplicate PDF hashes prevent schema v6; merge these paper IDs first: " +
                                     "; ".join(row['ids'] for row in duplicates))
                self._conn.execute("DROP INDEX IF EXISTS idx_papers_sha")
                self._conn.execute("CREATE UNIQUE INDEX idx_papers_sha ON papers(pdf_sha256) WHERE pdf_sha256 != ''")
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                return
            version = int(row["value"])
            if version < 2:
                columns = {column["name"] for column in self._conn.execute("PRAGMA table_info(relations)")}
                if "dst_url" not in columns:
                    self._conn.execute("ALTER TABLE relations ADD COLUMN dst_url TEXT DEFAULT ''")
            if version < 3:
                columns = {column["name"] for column in self._conn.execute("PRAGMA table_info(model_runs)")}
                for name, decl in _RUN_V3_COLUMNS:
                    if name not in columns:
                        self._conn.execute(f"ALTER TABLE model_runs ADD COLUMN {name} {decl}")
            if version < 4:
                columns = {column["name"] for column in self._conn.execute("PRAGMA table_info(model_runs)")}
                if "provider" not in columns:
                    self._conn.execute("ALTER TABLE model_runs ADD COLUMN provider TEXT DEFAULT 'legacy'")
            if version < 7:
                from carrel.profile import PRESETS
                columns = {column['name'] for column in self._conn.execute('PRAGMA table_info(papers)')}
                if 'profile' not in columns:
                    self._conn.execute("ALTER TABLE papers ADD COLUMN profile TEXT DEFAULT ''")
                self._conn.execute("UPDATE papers SET profile=? WHERE status != 'indexed'",
                                   (json.dumps(PRESETS['full'].to_dict()),))
                self._conn.execute("UPDATE papers SET profile=? WHERE status = 'indexed'",
                                   (json.dumps(PRESETS['indexed'].to_dict()),))
            if version < SCHEMA_VERSION:
                self._conn.execute("UPDATE schema_meta SET value=? WHERE key='version'", (str(SCHEMA_VERSION),))

    @property
    def version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- helpers
    @property
    def graph_revision(self) -> int:
        row = self.one("SELECT value FROM schema_meta WHERE key='graph_revision'")
        return int(row['value']) if row else 0

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -------------------------------------------------------------- papers
    def upsert_paper(self, meta: dict[str, Any]) -> None:
        from carrel.profile import stored_profile
        existing = self.get_paper(meta['id']) or {}
        profile = meta.get('profile') or existing.get('profile')
        if not profile:
            profile = stored_profile(meta).to_dict()
        if not isinstance(profile, str):
            profile = json.dumps(profile)
        authors = meta.get("authors") or []
        if not isinstance(authors, str):
            authors = json.dumps(authors, ensure_ascii=False)
        fields = {
            "id": meta["id"],
            "title": meta.get("title", ""),
            "title_zh": meta.get("title_zh", ""),
            "authors": authors,
            "year": meta.get("year"),
            "venue": meta.get("venue", ""),
            "abstract": meta.get("abstract", ""),
            "source_kind": meta.get("source_kind", ""),
            "source_url": meta.get("source_url", ""),
            "source_path": meta.get("source_path", ""),
            "pdf_sha256": meta.get("pdf_sha256", ""),
            "lang": meta.get("lang", "en"),
            "scope": meta.get("scope", "full"),
            "status": meta.get("status", "queued"),
            "profile": profile,
            "parse_backend": meta.get("parse_backend", ""),
            "parse_quality": meta.get("parse_quality", 0) or 0,
            "discipline": meta.get("discipline", ""),
            "field": meta.get("field", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        }
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "id")
        self.execute(
            f"INSERT INTO papers ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            list(fields.values()),
        )

    def set_status(self, paper_id: str, status: str, **extra: Any) -> None:
        sets = ["status=?"]
        params: list[Any] = [status]
        for key, value in extra.items():
            sets.append(f"{key}=?")
            params.append(value)
        params.append(paper_id)
        self.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id=?", params)

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM papers WHERE id=?", (paper_id,))
        return dict(row) if row else None

    def list_papers(
        self,
        *,
        q: str = "",
        discipline: str = "",
        field: str = "",
        tag: str = "",
        collection: str = "",
        status: str = "",
        sort: str = "recent",
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if q:
            where.append("(title LIKE ? OR title_zh LIKE ? OR abstract LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like]
        if discipline:
            where.append("discipline=?")
            params.append(discipline)
        if field:
            where.append("field=?")
            params.append(field)
        if status:
            where.append("status=?")
            params.append(status)
        if tag:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.paper_id=papers.id AND t.tag=?)")
            params.append(tag)
        if collection:
            where.append("EXISTS (SELECT 1 FROM paper_collections pc WHERE pc.paper_id=papers.id AND pc.collection_id=?)")
            params.append(collection)
        sql = "SELECT * FROM papers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        order = {
            "recent": "created_at DESC",
            "title": "title ASC",
            "year": "COALESCE(year, 0) DESC",
        }.get(sort, "created_at DESC")
        if sort == "rank" and collection:
            order = "COALESCE((SELECT pc.rank FROM paper_collections pc WHERE pc.paper_id=papers.id AND pc.collection_id=?), 2147483647), title"
            params.append(collection)
        sql += f" ORDER BY {order}"
        return [dict(r) for r in self.query(sql, params)]

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(str(destination)) as target:
            self._conn.backup(target)

    def collections(self, paper_id: str = "") -> list[dict[str, Any]]:
        where = " WHERE c.id IN (SELECT collection_id FROM paper_collections WHERE paper_id=?)" if paper_id else ""
        return [dict(r) for r in self.query(
            "SELECT c.*, COUNT(pc.paper_id) AS count FROM collections c "
            "LEFT JOIN paper_collections pc ON pc.collection_id=c.id" + where +
            " GROUP BY c.id ORDER BY c.sort, c.name, c.id", (paper_id,) if paper_id else ())]

    def notes_for(self, paper_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT n.*, COALESCE(c.name, '') AS collection_name FROM notes n "
            "LEFT JOIN collections c ON c.id=n.collection_id WHERE n.paper_id=? "
            "ORDER BY n.collection_id!='', c.sort, c.name", (paper_id,))]

    def delete_collection(self, collection_id: str) -> None:
        with self.transaction() as conn:
            collection = conn.execute("SELECT name FROM collections WHERE id=?", (collection_id,)).fetchone()
            if not collection:
                return
            for note in conn.execute("SELECT * FROM notes WHERE collection_id=?", (collection_id,)).fetchall():
                body = f"[{collection['name']}] {note['priority']}\n{note['body']}".strip()
                conn.execute("INSERT INTO notes(paper_id,collection_id,priority,body,source,source_ref,created_at,updated_at) "
                             "VALUES(?,'',?,?,'human',?,?,?) ON CONFLICT(paper_id,collection_id) DO UPDATE SET "
                             "body=notes.body || char(10) || char(10) || excluded.body,source='human',"
                             "source_ref=notes.source_ref || char(10) || excluded.source_ref",
                             (note["paper_id"], note["priority"], body, note["source_ref"], note["created_at"], note["updated_at"]))
            # General notes use an empty collection id, so this key has no FK.
            conn.execute("DELETE FROM notes WHERE collection_id=?", (collection_id,))
            conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))

    def merge_reading_data(self, source_id: str, target_id: str) -> None:
        """Keep reader-owned data when the pipeline removes a duplicate row."""
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO paper_collections(paper_id,collection_id,rank) "
                         "SELECT ?,collection_id,rank FROM paper_collections WHERE paper_id=?", (target_id, source_id))
            for source in conn.execute("SELECT * FROM notes WHERE paper_id=?", (source_id,)).fetchall():
                target = conn.execute("SELECT * FROM notes WHERE paper_id=? AND collection_id=?",
                                      (target_id, source["collection_id"])).fetchone()
                if target:
                    body = target["body"]
                    if source["body"] and source["body"] != body:
                        body = (body + "\n\n" + source["body"]).strip()
                    refs = "\n".join(dict.fromkeys((target["source_ref"] + "\n" + source["source_ref"]).splitlines())).strip()
                    conn.execute("UPDATE notes SET body=?,source_ref=?,source=?,priority=? WHERE paper_id=? AND collection_id=?",
                                 (body, refs, "human" if "human" in (source["source"], target["source"]) else "imported",
                                  target["priority"] or source["priority"], target_id, source["collection_id"]))
                else:
                    conn.execute("INSERT INTO notes(paper_id,collection_id,priority,body,source,source_ref,created_at,updated_at) "
                                 "VALUES(?,?,?,?,?,?,?,?)", (target_id, *(source[k] for k in
                                 ("collection_id", "priority", "body", "source", "source_ref", "created_at", "updated_at"))))

    # ---------------------------------------------------------------- tags
    def replace_tags(self, paper_id: str, tags: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM tags WHERE paper_id=? AND origin!='human'", (paper_id,))
            for t in tags:
                if not t.get("block_id"):
                    continue
                # A human-curated tag with the same label must keep its origin;
                # INSERT OR REPLACE would silently flip it back to 'llm'.
                exists_human = conn.execute(
                    "SELECT 1 FROM tags WHERE paper_id=? AND tag=? AND origin='human'",
                    (paper_id, t["tag"]),
                ).fetchone()
                if exists_human:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO tags(paper_id, tag, evidence, source_block, origin) "
                    "VALUES(?,?,?,?,?)",
                    (
                        paper_id,
                        t["tag"],
                        t.get("evidence", ""),
                        t["block_id"],
                        t.get("origin", "llm"),
                    ),
                )

    def tags_for(self, paper_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM tags WHERE paper_id=? ORDER BY tag", (paper_id,)
        )]

    def all_tags(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag ORDER BY n DESC"
        )]

    def tag_rows(self) -> list[dict[str, Any]]:
        """Every tag row in a stable order, for one-shot graph construction."""
        return [dict(r) for r in self.query(
            "SELECT paper_id, tag, evidence, source_block, origin FROM tags"
            " ORDER BY tag, paper_id"
        )]

    # ----------------------------------------------------------- relations
    def replace_relations(self, src_id: str, kind: str, rows: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM relations WHERE src_id=? AND kind=?", (src_id, kind))
            for r in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO relations(src_id, dst_id, kind, dst_title, dst_year,"
                    " dst_arxiv, dst_doi, dst_url, provider, coverage, rank, score, fetched_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        src_id,
                        r["dst_id"],
                        kind,
                        r.get("dst_title", ""),
                        r.get("dst_year"),
                        r.get("dst_arxiv", ""),
                        r.get("dst_doi", ""),
                        r.get("dst_url", ""),
                        r.get("provider", ""),
                        r.get("coverage", "complete"),
                        r.get("rank", 0),
                        r.get("score"),
                        r.get("fetched_at", ""),
                    ),
                )

    def relations_for(self, src_id: str, kind: str = "") -> list[dict[str, Any]]:
        if kind:
            rows = self.query(
                "SELECT * FROM relations WHERE src_id=? AND kind=? ORDER BY rank",
                (src_id, kind),
            )
        else:
            rows = self.query(
                "SELECT * FROM relations WHERE src_id=? ORDER BY kind, rank", (src_id,)
            )
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- jobs
    def create_job(self, job_id: str, paper_id: str, stage: str, now: str,
                   status: str = "running") -> None:
        self.execute(
            "INSERT INTO jobs(id, paper_id, stage, status, started_at) VALUES(?,?,?,?,?)",
            (job_id, paper_id, stage, status, now),
        )

    def finish_job(
        self, job_id: str, status: str, now: str, code: str = "", detail: str = ""
    ) -> None:
        self.execute(
            "UPDATE jobs SET status=?, finished_at=?, error_code=?, error_detail=?"
            " WHERE id=?",
            (status, now, code, detail, job_id),
        )

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)
        )]

    # ----------------------------------------------------------- model_runs
    _RUN_COLS = (
        "id", "paper_id", "task", "module", "status", "backend", "model",
        "tokens_in", "tokens_out", "duration_ms", "schema_ok", "qa_ok",
        "retry_of", "trace_dir", "error_code", "created_at",
        "thinking_tokens", "cache_read_tokens", "cache_write_tokens",
        "stop_reason", "max_tokens", "thinking", "truncated", "accepted", "cost_usd",
        "requested_model", "provider",
    )
    _RUN_NUMERIC = {
        "tokens_in", "tokens_out", "duration_ms", "schema_ok", "qa_ok",
        "thinking_tokens", "cache_read_tokens", "cache_write_tokens",
        "max_tokens", "truncated", "cost_usd",
    }

    def record_run(self, run: dict[str, Any]) -> None:
        cols = self._RUN_COLS
        values = []
        for c in cols:
            v = run.get(c)
            if v is None:
                v = 0 if c in self._RUN_NUMERIC else (-1 if c == "accepted" else "")
            elif isinstance(v, bool):
                v = int(v)
            values.append(v)
        self.execute(
            f"INSERT OR REPLACE INTO model_runs({', '.join(cols)}) "
            f"VALUES({', '.join('?' for _ in cols)})",
            values,
        )

    def runs_for(self, paper_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM model_runs WHERE paper_id=? ORDER BY created_at DESC", (paper_id,)
        )]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM model_runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    def usage_summary(self, *, paper_id: str | None = None,
                      since: str | None = None) -> dict[str, Any]:
        """Token and cost totals grouped by task (and by model within task).

        This is the report the cost work is judged by: the same papers imported
        before and after a change, compared row by row. ``accepted`` only means
        anything for repair runs (1 kept, 0 rolled back, -1 not applicable).
        """
        where, args = [], []
        if paper_id:
            where.append("paper_id=?"); args.append(paper_id)
        if since:
            where.append("created_at>=?"); args.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        agg = (
            "COUNT(*) AS calls, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, "
            "SUM(thinking_tokens) AS thinking_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_write_tokens) AS cache_write_tokens, SUM(cost_usd) AS cost_usd, "
            "SUM(duration_ms) AS duration_ms, SUM(truncated) AS truncated, "
            "SUM(CASE WHEN status NOT IN ('ok','rejected') THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN accepted=1 THEN 1 ELSE 0 END) AS accepted, "
            "SUM(CASE WHEN accepted=0 THEN 1 ELSE 0 END) AS rejected, "
            "SUM(CASE WHEN requested_model!='' AND model!=requested_model THEN 1 ELSE 0 END) AS substituted"
        )
        by_task = [dict(r) for r in self.query(
            f"SELECT task, {agg} FROM model_runs{clause} GROUP BY task ORDER BY cost_usd DESC, tokens_out DESC",
            args)]
        by_model = [dict(r) for r in self.query(
            f"SELECT task, model, {agg} FROM model_runs{clause} GROUP BY task, model ORDER BY task, cost_usd DESC",
            args)]
        by_provider = [dict(r) for r in self.query(
            f"SELECT COALESCE(NULLIF(provider, ''), 'legacy') AS provider, {agg} "
            f"FROM model_runs{clause} GROUP BY COALESCE(NULLIF(provider, ''), 'legacy') ORDER BY cost_usd DESC", args)]
        total = dict(self.one(f"SELECT {agg} FROM model_runs{clause}", args) or {})
        paper_clause = clause + (" AND " if clause else " WHERE ") + "paper_id NOT LIKE '@%'"
        papers = self.one(f"SELECT COUNT(DISTINCT paper_id) AS n FROM model_runs{paper_clause}", args)
        total["papers"] = int(papers["n"]) if papers else 0
        for row in by_task + by_model + by_provider + [total]:
            for k, v in list(row.items()):
                if v is None:
                    row[k] = 0
            row["cost_usd"] = round(float(row.get("cost_usd") or 0), 4)
        return {"total": total, "by_task": by_task, "by_model": by_model, "by_provider": by_provider}

    # ---------------------------------------------------------- categories
    def sync_categories(self, entries: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM categories")
            for e in entries:
                conn.execute(
                    "INSERT INTO categories(path, label, parent, sort) VALUES(?,?,?,?)",
                    (e["path"], e["label"], e.get("parent", ""), e.get("sort", 0)),
                )

    def list_categories(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT c.*, (SELECT COUNT(*) FROM papers p WHERE p.discipline=c.path) AS n"
            " FROM categories c WHERE c.parent='' ORDER BY c.sort, c.label"
        )
        out = []
        for r in rows:
            out.append(dict(r))
        return out

    def category_tree(self, *, only_used: bool = False) -> list[dict[str, Any]]:
        """Categories for display.

        The taxonomy is a constraint for the classifier, not a browsing aid: a
        fresh library shows only the categories that actually hold papers
        (``only_used=True``), otherwise every empty branch crowds the page.
        """
        rows = [dict(r) for r in self.query("SELECT * FROM categories ORDER BY sort, label")]
        tops = [r for r in rows if not r["parent"]]
        for top in tops:
            top["children"] = [r for r in rows if r["parent"] == top["path"]]
            for child in top["children"]:
                child["path"] = child["label"]
                child["count"] = self.one(
                    "SELECT COUNT(*) AS n FROM papers WHERE discipline=? AND field=?",
                    (top["path"], child["path"]),
                )["n"]
            top["count"] = self.one(
                "SELECT COUNT(*) AS n FROM papers WHERE discipline=?", (top["path"],)
            )["n"]
        if only_used:
            # Keep a top-level entry when the discipline itself is used, or when
            # any of its fields is; prune empty children either way.
            used = []
            for top in tops:
                if top["count"] > 0:
                    top["children"] = [c for c in top["children"] if c.get("count")]
                    used.append(top)
                elif any(c.get("count") for c in top["children"]):
                    top["children"] = [c for c in top["children"] if c.get("count")]
                    used.append(top)
            return used
        return tops

    # ---------------------------------------------------------- proposals
    def add_proposal(self, pid: str, kind: str, parent: str, label: str,
                     paper_id: str, reason: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO category_proposals(id, kind, parent, label, paper_id,"
            " reason, status) VALUES(?,?,?,?,?,?,'pending')",
            (pid, kind, parent, label, paper_id, reason),
        )

    def list_proposals(self, status: str = "pending") -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM category_proposals WHERE status=? ORDER BY label", (status,)
        )]

    def resolve_proposal(self, pid: str, status: str) -> None:
        self.execute("UPDATE category_proposals SET status=? WHERE id=?", (status, pid))
