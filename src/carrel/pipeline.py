"""End-to-end paper processing.

Stages: parse -> structure -> translate -> classify -> relate. Each stage writes
its own artifact and the paper status moves forward only when the stage that
produced it succeeded. Failures are visible; nothing is silently degraded.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from carrel import ids as ids_mod
from carrel.assess import assess
from carrel.classify import Classifier, Taxonomy
from carrel.config import AppConfig
from carrel.profile import (Profile, parse_profile, stored_profile, default_profile,
                            selected_section, section_identity, skipped_sections)
from carrel.coverage import paper_coverage, missing_capabilities
from carrel.runtime.context import RunContext, RunInterrupted, submit
from carrel.runtime.locks import paper_locks, content_locks, taxonomy_lock, path_key
from carrel.runtime.limits import Runtime
from carrel.runtime.retry import with_retry, classify, RetryableResponse
from carrel.model import ModelBackend, ModelError, ModelSchemaError, ModelTruncated
from carrel.models import (
    DocModel,
    ModuleExtract,
    PaperMeta,
    Summary,
)
from carrel.motivation.extract import ensure_motivation, reusable_motivation
from carrel.parse import (
    MinerUApiParser,
    ParseError,
    ParseLowQuality,
    ParseRemoteFailed,
    ParseUnavailable,
)
from carrel.relate import OpenAlexClient, RelationCollector, SciverseClient, link_to_library
from carrel.store.db import Database
from carrel.store.paths import (
    atomic_write_json,
    ensure_paper_dirs,
    paper_dir,
    read_json,
)
from carrel.structure import (
    Extractor,
    Summarizer,
    build_preview,
    build_sections,
    classify_heading,
    render_modules,
    sections_payload,
    summary_markdown,
)
from carrel.translate import Glossary, TermReviewer, Translator, build_glossary
from carrel.translate.translator import SKIP_TYPES
from carrel.titles import ensure_title

STAGE_ORDER = ("parsing", "structuring", "translating", "classifying", "relating")

#: Statuses that mean "nothing left to do": a plain (non-force) import of such a
#: paper returns immediately. ``partial`` and ``failed`` are not here on purpose
#: — re-importing them is how the user asks for the missing pieces.
_FINISHED = frozenset({"ready", "ready_with_warning", "indexed"})


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DuplicatePaper(Exception):
    """Raised when another record already holds these bytes; carries its id."""

    def __init__(self, existing_id: str) -> None:
        super().__init__(existing_id)
        self.existing_id = existing_id


@dataclass(slots=True)
class Progress:
    paper_id: str
    stage: str = "queued"
    detail: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


ProgressFn = Callable[[Progress], None]


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        proceed: bool = True,
        backend: ModelBackend | None = None,
        parser=None,
        relations: RelationCollector | None = None,
        runtime=None,
    ) -> None:
        self.config = config
        self.db = db
        self.data_dir = Path(config.data_dir)
        self.runtime = runtime or Runtime(config)
        from carrel.model.router import ProviderRouter

        self.backend: ModelBackend = backend or ProviderRouter.from_config(
            config, caps_path=self.data_dir / "model_caps.json")
        if hasattr(self.backend, "configure_runtime"):
            self.backend.configure_runtime(self.runtime)
        self.parser = parser or MinerUApiParser(
            token=config.mineru.token,
            base_url=config.mineru.base_url,
            model_version=config.mineru.model_version,
            poll_interval=config.mineru.poll_interval,
            timeout_s=config.mineru.timeout_s,
            download_max_mb=config.mineru.download_max_mb,
            runtime=self.runtime, data_dir=self.data_dir,
            daily_pages_soft_limit=config.mineru.daily_pages_soft_limit,
            over_limit=config.mineru.over_limit,
        )
        # Packaged taxonomy overlaid with the user's manual categories from
        # the data directory, so hand-added entries join the classifier's
        # vocabulary and survive upgrades.
        self.taxonomy = Taxonomy.layered(
            Path(__file__).resolve().parent / "taxonomy.toml",
            Path(config.data_dir) / "taxonomy.toml",
        )
        with taxonomy_lock:
            self.db.sync_categories(self.taxonomy.category_rows())
        self.relations = relations or RelationCollector(
            openalex=OpenAlexClient(mailto=config.openalex.mailto,
                                    api_key=config.openalex.effective_api_key, runtime=self.runtime),
            sciverse=SciverseClient(token=config.sciverse.token, runtime=self.runtime),
        )
        self.proceed = proceed
        self.defer_library = False

    # ------------------------------------------------------------------ run
    def process(
        self,
        raw_input: str,
        *,
        progress: ProgressFn | None = None,
        force: bool = False,
        title: str = "",
        profile: Profile | dict | str | None = None,
    ) -> str:
        resolved = ids_mod.resolve_input(raw_input)
        digest = ''
        if resolved.kind == 'local_path' and Path(resolved.locator).is_file():
            digest = ids_mod.sha256_file(Path(resolved.locator))
            resolved.paper_id = ids_mod.paper_id_for(sha256=digest)
        while True:
            if digest:
                existing = self.db.one('SELECT id FROM papers WHERE pdf_sha256=?', (digest,))
                if existing:
                    resolved.paper_id = existing['id']
            candidate = resolved.paper_id
            with paper_locks.hold((path_key(self.data_dir), candidate)):
                if digest:
                    current = self.db.one('SELECT id FROM papers WHERE pdf_sha256=?', (digest,))
                    if current and current['id'] != candidate:
                        continue  # release the old identity before acquiring the new one
                ctx = RunContext(force=force, profile=parse_profile(profile) if profile is not None else default_profile(self.data_dir))
                with ctx.stack:
                    try:
                        return self._process_resolved(resolved, progress=progress, force=force, title=title, ctx=ctx)
                    except DuplicatePaper as duplicate:
                        resolved.paper_id = duplicate.existing_id
                        # Reuse/resume the canonical record, including its paid
                        # artifacts and notes, after releasing both old locks.

    def _process_resolved(self, resolved, *, progress, force, title, ctx):
        paper_id, meta = self._prepare(resolved)
        if title.strip():
            meta.title = title.strip()

        existing = self.db.get_paper(paper_id)
        if existing:
            ctx.profile = stored_profile(existing).union(ctx.profile)
        meta.profile = ctx.profile.to_dict()
        if existing and existing.get("status") in _FINISHED and not force \
                and self._complete(paper_id, ctx.profile):
            if stored_profile(existing) != ctx.profile:
                self.db.execute('UPDATE papers SET profile=? WHERE id=?', (json.dumps(ctx.profile.to_dict()), paper_id))
            if title.strip() and title.strip() != existing.get("title"):
                self.db.execute("UPDATE papers SET title=?, updated_at=? WHERE id=?",
                                (title.strip(), _now(), paper_id))
            if ctx.profile.extract != 'none':
                ensure_title(self.config, self.db, self.backend, paper_id)
            return paper_id
        base = paper_dir(self.data_dir, paper_id)
        if ctx.profile.extract == 'none':
            if resolved.kind == 'local_path':
                meta.source_path = resolved.locator
                meta.pdf_sha256 = ids_mod.sha256_file(Path(resolved.locator))
                if not meta.title:
                    from carrel.sync.scanner import _metadata
                    source = Path(resolved.locator)
                    local = _metadata(source, source.parent, meta.pdf_sha256, {}, [])
                    meta.title = local['title']
                    meta.authors = local['authors']
                    meta.year = local['year']
                    meta.abstract = local['abstract']
            elif resolved.arxiv_id and not meta.title:
                import asyncio
                from carrel.source_titles import fetch_title
                async def lookup():
                    async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
                        return await fetch_title(resolved, client)
                meta.title = asyncio.run(lookup())
            if not meta.title:
                meta.title = paper_id
            meta.status = 'indexed'
            self.db.upsert_paper(meta.to_dict())
            if ctx.profile.relations:
                self._stage_relate(paper_id, meta, progress)
                self.db.set_status(paper_id, 'indexed')
            self._emit(progress, paper_id, 'indexed')
            return paper_id
        ensure_paper_dirs(self.data_dir, paper_id)
        meta.status = "parsing"
        self.db.upsert_paper(meta.to_dict())
        self._emit(progress, paper_id, "parsing")

        try:
            doc = self._stage_parse(resolved, paper_id, meta, progress, ctx)
            ctx.doc_fingerprint = doc.fingerprint()
            self._stage_structure(paper_id, meta, doc, progress, ctx)
            if ctx.profile.translate:
                self._stage_translate(paper_id, meta, doc, progress, ctx)
            self._stage_classify(paper_id, meta, progress, ctx)
        except DuplicatePaper:
            raise  # identity redirection, handled after releasing current locks
        except PipelineError as exc:
            self._fail(paper_id, exc.code, str(exc))
            raise
        except ParseUnavailable as exc:
            self._fail(paper_id, exc.code, str(exc))
            raise PipelineError(exc.code, str(exc)) from exc
        except ParseLowQuality as exc:
            self._fail(paper_id, exc.code, str(exc))
            raise PipelineError(exc.code, str(exc)) from exc
        except ParseRemoteFailed as exc:
            self._fail(paper_id, exc.code, str(exc))
            raise PipelineError(exc.code, str(exc)) from exc
        except ParseError as exc:
            self._fail(paper_id, exc.code, str(exc))
            raise PipelineError(exc.code, str(exc)) from exc
        except Exception as exc:
            code = getattr(exc, 'code', 'internal_error')
            self._fail(paper_id, code, str(exc))
            raise PipelineError(code, str(exc)) from exc

        # Relation failure only downgrades the status; the paper is still readable.
        covered = paper_coverage(base, ctx.doc_fingerprint)
        relation_warning = (self._stage_relate(paper_id, meta, progress)
                            if ctx.profile.relations and (force or 'relations' not in covered) else [])

        # Sections that failed to translate leave a hole the reader will hit, so
        # the paper is `partial`, not `ready`. The design's state machine requires
        # this and the UI surfaces it as a banner.
        failed_sections = list(ctx.partial_sections)
        warnings = list(relation_warning or [])
        missing = missing_capabilities(ctx.profile, paper_coverage(base, ctx.doc_fingerprint))
        if missing:
            warnings.append('尚未完成：' + '、'.join(sorted(missing)))
        if failed_sections or missing - {'relations'}:
            if failed_sections:
                warnings.append("未翻译的章节：" + "、".join(failed_sections))
            final = "partial"
        elif relation_warning:
            final = "ready_with_warning"
        else:
            final = "ready"
        self.db.set_status(paper_id, final)
        # A run that got this far superseded whatever failed last time; the old
        # error banner must not outlive the fix.
        (paper_dir(self.data_dir, paper_id) / "error.json").unlink(missing_ok=True)
        self._emit(progress, paper_id, final, {"warnings": warnings})
        return paper_id

    # -------------------------------------------------------------- helpers
    def _complete(self, paper_id: str, profile: Profile) -> bool:
        """Skip paid work only when artifacts satisfy the entire requested scope."""
        base = paper_dir(self.data_dir, paper_id)
        return not missing_capabilities(profile, paper_coverage(base))

    def _prepare(self, resolved) -> tuple[str, PaperMeta]:
        """Build the in-memory record, seeded from any existing row.

        Starting from defaults and then upserting would erase a paper's title,
        abstract, classification and creation time on every retry.
        """
        existing = self.db.get_paper(resolved.paper_id) or {}
        meta = PaperMeta(
            id=resolved.paper_id,
            title=existing.get("title") or "",
            title_zh=existing.get("title_zh") or "",
            authors=_json_list(existing.get("authors")),
            year=existing.get("year"),
            venue=existing.get("venue") or "",
            abstract=existing.get("abstract") or "",
            source_kind=existing.get("source_kind") or resolved.kind,
            source_url=resolved.source_url or existing.get("source_url") or "",
            source_path=existing.get("source_path") or "",
            pdf_sha256=existing.get("pdf_sha256") or "",
            lang=existing.get("lang") or "en",
            scope=existing.get("scope") or "full",
            status=existing.get("status") or "queued",
            parse_backend=existing.get("parse_backend") or "",
            parse_quality=existing.get("parse_quality") or 0.0,
            discipline=existing.get("discipline") or "",
            field=existing.get("field") or "",
            created_at=existing.get("created_at") or _now(),
            updated_at=_now(),
        )
        return resolved.paper_id, meta

    def _persist(self, paper_id: str, meta: PaperMeta, *, keep_status: bool = False) -> None:
        """Write metadata without clobbering fields another stage owns."""
        current = self.db.get_paper(paper_id) or {}
        payload = meta.to_dict()
        if keep_status and current.get("status"):
            payload["status"] = current["status"]
        # Never blank out a value that is already stored and still non-empty.
        for key in ("title", "abstract", "pdf_sha256", "parse_backend", "discipline", "field"):
            if not payload.get(key) and current.get(key):
                payload[key] = current[key]
        if not payload.get("created_at"):
            payload["created_at"] = current.get("created_at") or _now()
        self.db.upsert_paper(payload)

    def _emit(self, progress: ProgressFn | None, paper_id: str, stage: str,
              detail: dict[str, Any] | None = None) -> None:
        self.runtime.check_stopped()
        if progress is not None:
            progress(Progress(paper_id=paper_id, stage=stage, detail=detail or {}))

    def _fail(self, paper_id: str, code: str, detail: str) -> None:
        self.db.execute(
            "UPDATE papers SET status='failed', updated_at=? WHERE id=?",
            (_now(), paper_id),
        )
        base = paper_dir(self.data_dir, paper_id)
        base.mkdir(parents=True, exist_ok=True)
        atomic_write_json(base / "error.json", {"code": code, "detail": detail})

    def _record_run(self, **kwargs) -> str:
        from carrel.store.runs import record_run

        return record_run(self.config, self.db, self.backend, **kwargs)

    # -------------------------------------------------------------- stage 1
    def _stage_parse(self, resolved, paper_id: str, meta: PaperMeta,
                     progress: ProgressFn | None, ctx: RunContext) -> DocModel:
        base = ensure_paper_dirs(self.data_dir, paper_id)
        pdf_path = base / "source.pdf"

        if resolved.kind == "local_path":
            src = Path(resolved.locator)
            if not src.is_file():
                raise PipelineError("input_not_found", f"local PDF not found: {src}")
            pdf_path.write_bytes(src.read_bytes())
            meta.source_path = str(src)
        elif pdf_path.is_file() and pdf_path.stat().st_size > 0:
            pass  # already downloaded on a previous attempt
        else:
            url = resolved.source_url
            if resolved.kind == "arxiv" or resolved.kind == "hf":
                url = ids_mod.arxiv_pdf_url(resolved.arxiv_id or resolved.locator)
            self._download(url, pdf_path)
            meta.source_url = read_json(base / "download_source.json", {}).get("url", url)

        meta.pdf_sha256 = ids_mod.sha256_file(pdf_path)
        with pdf_path.open('rb') as source:
            if not source.read(5).startswith(b'%PDF-'):
                raise PipelineError('invalid_pdf', '本地文件不是有效的 PDF，请检查源文件。')
        ctx.stack.enter_context(content_locks.hold((path_key(self.data_dir), meta.pdf_sha256)))
        # A metadata-only local sync can reserve the same SHA without taking
        # pipeline locks. Serialize its transaction with identity reservation.
        with self.db.transaction() as conn:
            twins = conn.execute("SELECT id, status FROM papers WHERE pdf_sha256=? AND id!=?",
                                 (meta.pdf_sha256, paper_id)).fetchall()
            if not twins:
                conn.execute('UPDATE papers SET pdf_sha256=? WHERE id=?', (meta.pdf_sha256, paper_id))
        for twin in twins:
            # Never delete a canonical record: it can contain paid checkpoints,
            # reader notes, or an active worker waiting for this content lock.
            self.db.merge_reading_data(paper_id, twin['id'])
            self.db.execute('UPDATE jobs SET paper_id=? WHERE paper_id=?', (twin['id'], paper_id))
            self.db.execute('DELETE FROM papers WHERE id=?', (paper_id,))
            shutil.rmtree(base, ignore_errors=True)
            raise DuplicatePaper(twin['id'])
        # Reserve the content identity before the first paid parser request.
        self._persist(paper_id, meta, keep_status=True)

        def on_progress(stage: str, payload: dict[str, Any]) -> None:
            self._emit(progress, paper_id, "parsing", {"stage": stage, **payload})

        # A finished parse of these exact bytes is reused: MinerU is the slowest
        # external call in the pipeline and its output does not change between
        # runs. ``force`` re-parses so a parser upgrade can be picked up.
        doc = None
        if not ctx.force:
            saved = read_json(base / "doc.json", None)
            if isinstance(saved, dict) and saved.get("pdf_sha256") == meta.pdf_sha256 and saved.get("blocks"):
                doc = DocModel.from_dict(saved)
                self._emit(progress, paper_id, "parsing", {"stage": "reused", "blocks": len(doc.blocks)})
        if doc is None:
            if ctx.force:
                (base / 'parse_raw' / 'upload.json').unlink(missing_ok=True)
            doc = self.parser.parse(
                pdf_path, out_dir=base / "parse_raw", data_id=paper_id, progress=on_progress
            )
        meta.parse_backend = doc.backend
        meta.parse_quality = doc.quality
        meta.updated_at = _now()
        # Persist with keep_status: the run has already advanced past `parsing`,
        # and writing this stale status back would visibly move it backwards.
        self._persist(paper_id, meta, keep_status=True)
        atomic_write_json(base / "doc.json", {**doc.to_dict(), "pdf_sha256": meta.pdf_sha256})
        return doc

    def _download(self, url: str, dest: Path) -> None:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or '').lower()
        arxiv = host in ('arxiv.org', 'export.arxiv.org', 'www.arxiv.org')
        resource = self.runtime.pool('download:arxiv' if arxiv else 'download')
        sources = [url]
        if host in ('arxiv.org', 'www.arxiv.org'):
            sources.append(url.replace(host, 'export.arxiv.org', 1))
        tmp = dest.with_suffix('.part')
        for index, source in enumerate(sources):
            def attempt():
                try:
                    with httpx.stream('GET', source, timeout=120, follow_redirects=True) as resp:
                        resp.raise_for_status()
                        size = 0
                        with tmp.open('wb') as fh:
                            for chunk in resp.iter_bytes():
                                size += len(chunk)
                                if size > self.config.mineru.download_max_mb * 1024 * 1024:
                                    raise PipelineError('download_too_large', 'PDF exceeds configured size limit')
                                fh.write(chunk)
                        with tmp.open('rb') as fh:
                            if not fh.read(5).startswith(b'%PDF-'):
                                raise RetryableResponse('download returned HTML or invalid PDF content')
                        tmp.replace(dest)
                        atomic_write_json(dest.parent / 'download_source.json', {'url': str(resp.url)})
                finally:
                    tmp.unlink(missing_ok=True)
            try:
                with_retry(attempt, self.runtime.policy, resource=resource)
                return
            except Exception as exc:
                retryable = classify(exc) == 'retry'
                if retryable and index + 1 < len(sources):
                    continue
                if isinstance(exc, PipelineError):
                    raise
                error = PipelineError('download_failed', f'could not fetch {source}: {exc}')
                error.retryable = retryable
                raise error from exc

    # -------------------------------------------------------------- stage 2
    def _stage_structure(self, paper_id: str, meta: PaperMeta, doc: DocModel,
                         progress: ProgressFn | None, ctx: RunContext) -> None:
        base = paper_dir(self.data_dir, paper_id)
        self.db.set_status(paper_id, "structuring")
        self._emit(progress, paper_id, "structuring")

        sections, unassigned = build_sections(doc.blocks)
        section_rows = [s.to_dict() for s in sections]
        atomic_write_json(base / "sections.json", sections_payload(sections, unassigned))

        # Title and abstract come from the source document, before any model call,
        # so every later stage has real metadata to work with. keep_status: this
        # meta object still carries the stale "parsing" from creation.
        _fill_meta_from_extracts(meta, doc, section_rows, {}, Summary())
        self._persist(paper_id, meta, keep_status=True)

        by_id = doc.blocks_by_id()
        extractor = Extractor(self.backend)
        extracts: dict[str, ModuleExtract] = {}
        skipped: list[str] = []

        # Sections are independent, and each one is a single slow call. Running
        # them serially makes a 19-section paper take ten minutes; a small worker
        # pool cuts that to the cost of the slowest few.
        jobs: list[tuple[Section, list[Block]]] = []
        all_jobs = [(s, [by_id[b] for b in s.block_ids if b in by_id]) for s in sections
                    if selected_section(s, 'all') and any(b in by_id for b in s.block_ids)]
        for section in sections:
            if not selected_section(section, ctx.profile.extract):
                continue
            blocks = [by_id[b] for b in section.block_ids if b in by_id]
            if blocks:
                jobs.append((section, blocks))

        # Progress manifest: the UI polls against these totals so "翻译 7/13"
        # counts the same sections this run actually intends to process.
        translate_total = sum(
            1 for section, _ in jobs
            if ctx.profile.translate and selected_section(section, ctx.profile.extract, translation=True)
        )
        atomic_write_json(base / "progress_meta.json", {
            "modules_total": len(jobs),
            "translations_total": translate_total,
        })

        # Resume: a module extracted from this exact doc is reused; only the
        # sections without a valid artefact go back to the model. A key match
        # alone is not enough — the fingerprint covers block text, but a section
        # can be renamed or re-kinded without changing any block, and then the
        # file under that key describes a different chapter. Title and span
        # must agree with the section the key now denotes.
        fingerprint = doc.fingerprint()
        planned = {_module_key(section, all_jobs): (section, blocks) for section, blocks in jobs}
        reusable: dict[str, ModuleExtract] = {}
        if not ctx.force:
            for key, ex in _load_extracts(base, fingerprint=fingerprint).items():
                job = planned.get(key)
                if job is None:
                    continue
                section, blocks = job
                expected_span = [blocks[0].id, blocks[-1].id] if blocks else []
                if (ex.kind == section.kind
                        and ex.title_original.strip() == section.title.strip()
                        and list(ex.block_span) == expected_span):
                    reusable[key] = ex
        # Sections that produced nothing usable last time (a lone caption, a
        # table stub) are not re-sent either: the skip is recorded with the doc
        # fingerprint and only a different doc, or ``force``, lifts it.
        skipped_before: set[str] = set()
        if not ctx.force:
            skip_saved = read_json(base / "extract_skipped.json", None)
            skipped_before = skipped_sections(skip_saved, sections, fingerprint)
        pending = [job for job in jobs
                   if _module_key(job[0], all_jobs) not in reusable
                   and section_identity(job[0]) not in skipped_before]
        extracts.update(reusable)
        if reusable or skipped_before:
            self._emit(progress, paper_id, "structuring",
                       {"reused_modules": len(reusable), "pending": len(pending),
                        "skipped_before": sorted(skipped_before)})

        # The preview rides in the same pool as the first extractions: one cheap
        # call that makes the overview card readable ~3 minutes earlier. A
        # preview already on disk is never regenerated on a resume.
        need_preview = not (base / "preview.json").is_file()

        def run_preview():
            intro = ""
            for section, blocks in jobs:
                if section.kind == "introduction":
                    intro = " ".join(b.text for b in blocks if b.type != "formula")
                    break
            return build_preview(
                self.backend, title=meta.title, abstract=meta.abstract,
                intro_text=intro[:4000],
            )

        def extract_one(job: tuple[Section, list[Block]]):
            section, blocks = job
            try:
                return extractor.extract_section(section, blocks)
            except ModelTruncated as exc:
                self._record_run(paper_id=paper_id, task="extract",
                                 module=_module_key(section, all_jobs), status="truncated",
                                 meta=_reply_meta(exc), error_code=exc.code, schema_ok=0)
                return extractor.extract_section(section, blocks, compact=True)

        workers = max(1, min(self.config.max_parallel_modules, len(pending) or 1))
        extract_errors: list[tuple[str, ModelError]] = []
        interrupted = False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            preview_future = submit(pool, run_preview) if need_preview else None
            futures = {submit(pool, extract_one, job): job for job in pending}
            # The owning thread already holds the paper lock. Run the short
            # title task here while preview/extractions run in the pool.
            try:
                ensure_title(self.config, self.db, self.backend, paper_id)
            except RunInterrupted:
                interrupted = True  # still persist other completed paid work below
            meta.title_zh = (self.db.get_paper(paper_id) or {}).get('title_zh') or ''
            preview, preview_meta = {}, {}
            preview_error = ""
            if preview_future is not None:
                try:
                    preview, preview_meta = preview_future.result(
                        timeout=self.config.model.timeout_s * 3)
                except RunInterrupted:
                    interrupted = True
                except FuturesTimeout:
                    # The pool's __exit__ waits for the thread anyway, so a
                    # second, unbounded wait costs no extra wall clock — and the
                    # call is billed whether or not we looked at the answer.
                    preview_error = "timeout"
                    try:
                        preview, preview_meta = preview_future.result()
                    except RunInterrupted:
                        interrupted = True
                    except Exception as exc:  # noqa: BLE001
                        preview, preview_meta = {}, {"error": str(exc)[:200]}
                except Exception as exc:  # noqa: BLE001 - the preview is a nicety, never fatal
                    preview, preview_meta = {}, {"error": str(exc)[:200] or type(exc).__name__}
            # The preview call was invisible in the run log for its whole life;
            # record it like every other call so the bill adds up.
            if preview_meta or preview_error:
                self._record_run(paper_id=paper_id, task="preview",
                                 status="ok" if preview else "failed",
                                 error_code=(str(preview_meta.pop("error", "")) or preview_error)[:200],
                                 meta=preview_meta)
            if preview:
                atomic_write_json(base / "preview.json", preview)
            for future in as_completed(futures):
                section, _blocks = futures[future]
                try:
                    extract, run_meta = future.result()
                except RunInterrupted:
                    interrupted = True
                    continue
                except ModelSchemaError as exc:
                    # A sparse section (a lone figure caption, a table stub) can
                    # yield nothing usable. Skip it; only fail if none succeeds.
                    # The reply was still paid for, so its accounting is kept.
                    self._record_run(paper_id=paper_id, task="extract",
                                     module=section.kind, status="skipped",
                                     error_code=str(exc)[:200], schema_ok=0,
                                     meta=_reply_meta(exc))
                    skipped.append(section_identity(section))
                    continue
                except ModelError as exc:
                    self._record_run(paper_id=paper_id, task="extract",
                                     module=section.kind,
                                     status="truncated" if isinstance(exc, ModelTruncated) else "failed",
                                     error_code=(exc.code or "model_error")[:200],
                                     meta=_reply_meta(exc))
                    extract_errors.append((section.kind, exc))
                    continue
                key = _module_key(section, all_jobs)
                extracts[key] = extract
                atomic_write_json(base / "modules" / f"{key}.json",
                                  {**extract.to_dict(), "doc_fingerprint": fingerprint})
                self._record_run(paper_id=paper_id, task="extract",
                                 module=section.kind, meta=run_meta)

        # Drain the pool before failing: other completed sections and their
        # paid calls must survive so the next run can reuse them.
        if interrupted:
            raise RunInterrupted()
        if extract_errors:
            kind, exc = extract_errors[0]
            raise PipelineError("extract_failed", f"section {kind}: {exc}") from exc

        if not extracts:
            raise PipelineError("extract_empty", "未取得所选章节的笔记。请检查 PDF 章节；缺少摘要、引言和结论时可改选笔记档位。")
        all_skipped = sorted(set(skipped) | skipped_before)
        if all_skipped:
            atomic_write_json(base / "extract_skipped.json",
                              {"schema_version": 2, "sections": all_skipped, "doc_fingerprint": fingerprint})
        else:
            (base / "extract_skipped.json").unlink(missing_ok=True)

        # Modules that belong to a section this doc no longer has would still
        # be read by later stages and shown in the reader; remove them.
        _prune_sections(base / 'modules', sections, translation=False)
        extracts.update(_load_extracts(base, fingerprint=fingerprint))
        # Completion order must not reorder the next model prompt. Use the
        # same stable order as loading saved extracts during a resumed run.
        extracts = {key: extracts[key] for key in sorted(extracts)}

        # The summary is reused when it was built from exactly this set of
        # extracts — not "when nothing is pending", which never holds for a
        # paper with a permanently sparse section and re-bought the summary on
        # every retry. A cut-off or degraded summary is not a finished one.
        extracts_fp = _extracts_fingerprint(extracts)
        summary_saved = None if ctx.force else read_json(base / "summary.json", None)
        summary_ok = (
            isinstance(summary_saved, dict)
            and summary_saved.get("doc_fingerprint") == fingerprint
            and summary_saved.get("extracts_fingerprint") == extracts_fp
            and summary_saved.get("one_line_source", "model") == "model"
        )
        if summary_ok:
            summary = _load_summary(base)
            self._emit(progress, paper_id, "structuring", {"reused_summary": True})
        else:
            summarizer = Summarizer(self.backend)
            summary, run_meta = summarizer.build(
                title=meta.title or paper_id,
                abstract=meta.abstract,
                extracts=extracts,
                scope=meta.scope,
            )
            atomic_write_json(base / "summary.json",
                              {**summary.to_dict(), "doc_fingerprint": fingerprint,
                               "extracts_fingerprint": extracts_fp})
            # A summary that fell back to stitched digests because the reply was
            # cut off (or never became an object) is not a success, whatever the
            # HTTP status said.
            degraded = str(run_meta.pop("error", ""))
            self._record_run(paper_id=paper_id, task="summarize", meta=run_meta,
                             status="truncated" if summary.one_line_source == "truncated"
                             else ("degraded" if degraded else "ok"),
                             error_code=degraded[:200],
                             schema_ok=0 if summary.one_line_source in ("fallback", "truncated") else 1)

        # Derive any remaining metadata from the source text (never from the
        # model's summary), then persist without disturbing the run status.
        _fill_meta_from_extracts(meta, doc, section_rows, extracts, summary)
        self._persist(paper_id, meta, keep_status=True)

        md = ["# " + (meta.title or paper_id), "", summary_markdown(summary), "",
              "# 分模块笔记", "", render_modules(extracts)]
        (base / "notes.md").write_text("\n".join(md), encoding="utf-8")

    # -------------------------------------------------------------- stage 3
    def _stage_translate(self, paper_id: str, meta: PaperMeta, doc: DocModel,
                         progress: ProgressFn | None, ctx: RunContext) -> None:
        base = paper_dir(self.data_dir, paper_id)
        self.db.set_status(paper_id, "translating")
        self._emit(progress, paper_id, "translating")

        sections = (read_json(base / "sections.json", {}) or {}).get("sections", [])
        by_id = doc.blocks_by_id()
        # Only extracts from this doc: a chapter the paper no longer has must not
        # seed the term table.
        extracts = _load_extracts(base, fingerprint=ctx.doc_fingerprint)

        # The glossary call is a single model request that gates every module of
        # translation. A non-empty table on disk is reused as-is: the user may
        # have edited it, and regenerating it costs a call and risks replacing a
        # good table with a worse (or, after a truncated reply, empty) one.
        glossary_path = base / "glossary.json"
        existing_glossary = Glossary.load(glossary_path)
        if existing_glossary.terms and not ctx.force:
            glossary = existing_glossary
        else:
            glossary_started = time.time()
            glossary, glossary_meta = build_glossary(
                self.backend, title=meta.title, abstract=meta.abstract, extracts=extracts
            )
            glossary_meta.setdefault("duration_ms", int((time.time() - glossary_started) * 1000))
            error = str(glossary_meta.pop("error", ""))
            self._record_run(paper_id=paper_id, task="glossary", meta=glossary_meta,
                             status="failed" if error else "ok",
                             error_code=error[:200], schema_ok=0 if error else 1)
            # Never let a failed regeneration wipe a table that worked before.
            # ``build_glossary`` seeds from extract definitions even when the
            # model call failed, so a non-empty result is not proof of success —
            # the recorded error is. The seeded table is still used in memory.
            if existing_glossary.terms and (error or not glossary.terms):
                glossary = existing_glossary
            else:
                glossary.save(glossary_path)

        translator = Translator(self.backend)
        tables: dict[str, dict[str, str]] = {}
        warnings: list[str] = []
        failed_sections: list[str] = []
        transient_sections: list[str] = []
        attempted = 0

        # Build the module job list first so keys are assigned deterministically
        # regardless of thread completion order.
        jobs: list[tuple[str, str, str, list[dict[str, Any]]]] = []
        used_keys: set[str] = set()
        for section in sections:
            kind = section["kind"]
            title = section.get("title") or ""
            if not selected_section(section, 'all', translation=True):
                continue
            blocks = [
                {"id": b.id, "text": b.text, "type": b.type}
                for b in (by_id[i] for i in section["block_ids"] if i in by_id)
            ]
            if not blocks:
                continue
            key = kind
            suffix = 2
            while key in used_keys:
                key = f"{kind}-{suffix}"
                suffix += 1
            used_keys.add(key)
            if not selected_section(section, ctx.profile.extract, translation=True):
                continue
            jobs.append((key, kind, title, blocks))

        progress_meta = read_json(base / "progress_meta.json", {}) or {}
        progress_meta["translations_total"] = len(jobs)
        atomic_write_json(base / "progress_meta.json", progress_meta)

        # Resume: a module whose translation on disk came from this exact doc,
        # carries the same title, and has every block translated is kept; only
        # the rest are sent. This is what makes a retry of a `partial` paper
        # cost one module, not all. The title check matters: the reader looks a
        # translation's chapter up by title, and a renamed section with an old
        # title falls out of the table of contents.
        fingerprint = doc.fingerprint()
        reused_keys: list[str] = []
        if not ctx.force:
            pending_jobs = []
            for job in jobs:
                key, kind, title, blocks = job
                saved = read_json(base / "translation" / f"{key}.json", None)
                if (isinstance(saved, dict)
                        and saved.get("doc_fingerprint") == fingerprint
                        and str(saved.get("title") or "").strip() == title.strip()
                        and isinstance(saved.get("translations"), dict)
                        and all(saved["translations"].get(b["id"]) for b in blocks
                                if b.get("type") not in SKIP_TYPES)):
                    tables[key] = dict(saved["translations"])
                    reused_keys.append(key)
                else:
                    pending_jobs.append(job)
            jobs = pending_jobs
        if reused_keys:
            self._emit(progress, paper_id, "translating",
                       {"reused_modules": len(reused_keys), "pending": len(jobs)})

        def translate_module_job(job):
            """Checkpoint complete blocks and bisect truncated requests."""
            key, kind, title, blocks = job
            checkpoint_path = base / "translation" / "_checkpoints" / f"{key}.json"
            saved = None if ctx.force else read_json(checkpoint_path, None)
            merged: dict[str, str] = {}
            if (isinstance(saved, dict) and saved.get("doc_fingerprint") == fingerprint
                    and saved.get("title") == title
                    and saved.get("block_ids") == [b["id"] for b in blocks]
                    and isinstance(saved.get("translations"), dict)):
                merged = {bid: text for bid, text in saved["translations"].items()
                          if isinstance(text, str) and text.strip()}
            task_settings = getattr(self.backend, "task_settings", self.config.model.for_task)
            ceiling = task_settings("translate")[2]
            limit = min(MAX_TRANSLATE_CHARS, max(1000, ceiling // 2))
            pending = [(chunk, 0) for chunk in _split_for_translation(blocks, limit)]
            missing_retries: set[str] = set()
            previous_tail = ""
            calls = completed = 0
            failed = False
            transient_failure = False
            while pending:
                full_chunk, depth = pending.pop(0)
                chunk = [b for b in full_chunk if b["id"] not in merged]
                if not chunk:
                    previous_tail = _tail_text(full_chunk)
                    continue
                result = None
                for attempt in range(2):
                    calls += 1
                    try:
                        result = translator.translate_module(
                            kind=kind, blocks=chunk, glossary=glossary,
                            context=_context(meta, previous_tail),
                        )
                        break
                    except ModelTruncated as exc:
                        self._record_run(paper_id=paper_id, task="translate", module=key,
                                         status="truncated", meta=_reply_meta(exc),
                                         error_code=exc.code, schema_ok=0)
                        # Keep source blocks intact, including formulas and tables.
                        # At most three subdivisions per original slice.
                        if len(chunk) > 1 and depth < 3:
                            sizes = [len(str(b.get("text") or "")) for b in chunk]
                            half = sum(sizes) / 2
                            used = 0
                            boundaries = []
                            for index, size in enumerate(sizes[:-1], 1):
                                used += size
                                boundaries.append((abs(used - half), index))
                            split = min(boundaries)[1]
                            pending[:0] = [(chunk[:split], depth + 1), (chunk[split:], depth + 1)]
                        else:
                            failed = True
                            warnings.append(f"{key}: {exc}")
                        break
                    except ModelError as exc:
                        retryable = "empty" in str(exc).lower() or "timed out" in str(exc).lower()
                        meta_ = _reply_meta(exc)
                        if attempt == 0 and retryable:
                            self._record_run(paper_id=paper_id, task="translate",
                                             module=key, status="retry", meta=meta_,
                                             error_code=str(exc)[:200])
                            continue
                        self._record_run(paper_id=paper_id, task="translate",
                                         module=key, meta=meta_, status="failed",
                                         error_code=(exc.code or "model_error")[:200])
                        warnings.append(f"{key}: {exc}")
                        failed = True
                        transient_failure = transient_failure or classify(exc) == 'retry'
                        break
                if result is None:
                    continue
                for run in result.runs:
                    self._record_run(paper_id=paper_id, task=run["task"], module=key,
                                     meta=run, status=str(run.get("status") or "ok"),
                                     error_code=str(run.get("error_code") or ""),
                                     qa_ok=int(result.qa.ok))
                merged.update({bid: text for bid, text in result.translations.items()
                               if isinstance(text, str) and text.strip()})
                missing = [b["id"] for b in chunk
                           if b.get("type") not in SKIP_TYPES and b["id"] not in merged]
                if missing:
                    exhausted = [bid for bid in missing if bid in missing_retries]
                    retry_blocks = [b for b in chunk
                                    if b["id"] in missing and b["id"] not in missing_retries]
                    pending[:0] = [([block], depth) for block in retry_blocks]
                    missing_retries.update(missing)
                    if exhausted:
                        failed = True
                        warnings.append(f"{key}: missing translated blocks: {', '.join(exhausted)}")
                completed += 1
                atomic_write_json(checkpoint_path, {
                    "doc_fingerprint": fingerprint, "title": title,
                    "block_ids": [b["id"] for b in blocks], "translations": merged,
                })
                previous_tail = _tail_text(chunk)
            # QA must cover every slice, including checkpointed blocks.
            qa = translator._check(blocks, merged, glossary)
            return key, kind, title, merged, qa, completed, calls, failed, transient_failure

        workers = max(1, min(self.config.max_parallel_modules, len(jobs) or 1))
        interrupted = False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [submit(pool, translate_module_job, job) for job in jobs]
            for future in as_completed(futures):
                try:
                    key, kind, title, merged, qa, n_slices, calls, failed, transient_failure = future.result()
                except RunInterrupted:
                    interrupted = True
                    continue
                attempted += calls
                if failed:
                    failed_sections.append(key)
                    if transient_failure:
                        transient_sections.append(key)
                    continue
                if not merged:
                    continue
                tables[key] = merged
                needs = sorted(qa.failed_blocks()) if qa else []
                atomic_write_json(base / "translation" / f"{key}.json", {
                    "kind": kind,
                    "title": title,
                    "translations": merged,
                    "qa": qa.to_dict() if qa else {"ok": True, "issues": []},
                    "needs_review": needs,
                    "slices": n_slices,
                    "doc_fingerprint": fingerprint,
                })
                (base / "translation" / "_checkpoints" / f"{key}.json").unlink(missing_ok=True)

        if interrupted:
            raise RunInterrupted()
        if not tables:
            error = PipelineError("translate_empty", "nothing was translated")
            error.retryable = bool(transient_sections)
            raise error

        # Translations of chapters this doc no longer has would be rendered in
        # the reader (and counted by the progress bar) as if they existed.
        _prune_sections(base / 'translation', sections, translation=True)

        # A section that failed is a hole in the reading experience, so the paper
        # must not be reported as fully ready. This is the `partial` state the
        # design calls for; it must be visible, not hidden behind a green dot.
        if failed_sections:
            atomic_write_json(base / "translate_warnings.json", {
                "failed_sections": failed_sections,
                "retryable": bool(transient_sections),
                "transient_sections": transient_sections,
                "attempted": attempted,
                "completed": list(tables),
                "messages": warnings,
            })
            self.db.set_status(paper_id, "partial")
            ctx.partial_sections = failed_sections
        else:
            # A successful rerun must clear the previous attempt's marker, or
            # the stale failure flag outlives the fix (and any reader of this
            # file — including a later stage-check — keeps reporting partial).
            (base / "translate_warnings.json").unlink(missing_ok=True)

        # The whole-paper term review only has new information when a module
        # was (re)translated this run; a pure resume with nothing pending would
        # just re-review text it already reviewed.
        if jobs or ctx.force or not (base / "translation" / "_term_review.json").is_file():
            tables = {key: tables[key] for key in sorted(tables)}
            review = TermReviewer(self.backend).review(
                glossary=glossary, translations_by_kind=tables
            )
            if review.run_meta:
                error = str(review.run_meta.pop("error", ""))
                self._record_run(paper_id=paper_id, task="term_review", meta=review.run_meta,
                                 status="failed" if error else "ok", error_code=error[:200])
            atomic_write_json(base / "translation" / "_term_review.json", review.to_dict())
            for key, table in tables.items():
                path = base / "translation" / f"{key}.json"
                existing = read_json(path, {}) or {}
                existing["translations"] = table
                atomic_write_json(path, existing)

    # -------------------------------------------------------------- stage 4
    def _stage_classify(self, paper_id: str, meta: PaperMeta,
                        progress: ProgressFn | None, ctx: RunContext) -> None:
        base = paper_dir(self.data_dir, paper_id)
        self.db.set_status(paper_id, "classifying")
        self._emit(progress, paper_id, "classifying")
        fp = ctx.doc_fingerprint

        # Resume: classification and assessment each depend only on the extracts
        # and summary, which were reused or rebuilt upstream, so each is gated on
        # its *own* artefact from this doc. Sharing one gate let a paper whose
        # assessment had failed never get one without a full force rerun.
        saved_cls = read_json(base / "classification.json", None)
        human_cls = (isinstance(saved_cls, dict) and saved_cls.get('origin') == 'human'
                     and bool(saved_cls.get('categories') or saved_cls.get('discipline')))
        reuse_cls = human_cls or (not ctx.force and isinstance(saved_cls, dict)
                                  and saved_cls.get("doc_fingerprint") == fp)
        saved_assess = None if ctx.force else read_json(base / "assessment.json", None)
        reuse_assess = isinstance(saved_assess, dict) and saved_assess.get("doc_fingerprint") == fp
        reuse_motivation = not ctx.force and reusable_motivation(base, fp) is not None
        if reuse_cls and ctx.profile.classify:
            from carrel.classify.migration import project_classification
            with taxonomy_lock:
                vocabulary = Taxonomy.layered(Path(__file__).parent / 'taxonomy.toml', self.config.data_dir / 'taxonomy.toml')
                saved_cls = project_classification(saved_cls, vocabulary.renames)
            # The artefact is only half the result: tags and the discipline/field
            # columns live in SQLite, and the reader treats SQLite as the source.
            # Replay them so a rebuilt database (or a crash between the two
            # writes) does not leave the paper unclassified while the file says
            # otherwise. replace_tags keeps human tags.
            self.db.replace_tags(paper_id, [t for t in (saved_cls.get("tags") or [])
                                            if isinstance(t, dict)])
            self.db.execute(
                "UPDATE papers SET discipline=?, field=?, updated_at=? WHERE id=?",
                (str(saved_cls.get("discipline") or ""), str(saved_cls.get("field") or ""),
                 _now(), paper_id),
            )
            self._emit(progress, paper_id, "classifying", {"reused": True})
        if ((reuse_cls or not ctx.profile.classify) and (reuse_assess or not ctx.profile.assess)
                and (reuse_motivation or not ctx.profile.motivation)):
            return

        extracts = _load_extracts(base, fingerprint=fp)
        summary = _load_summary(base)
        classifier = Classifier(self.backend)
        with taxonomy_lock:
            taxonomy = Taxonomy.layered(Path(__file__).resolve().parent / 'taxonomy.toml', self.data_dir / 'taxonomy.toml')

        def run_classify():
            return classifier.classify(
                taxonomy=taxonomy,
                title=meta.title or paper_id,
                summary=summary,
                extracts=extracts,
            )

        def run_assess():
            return assess(
                self.backend,
                title=meta.title or paper_id,
                summary=summary,
                extracts=extracts,
            )

        # All three tasks depend on the same saved extracts and summary. Drain
        # the optional futures before raising a classification failure, so their
        # paid results and accounting are retained.
        classify_error: ModelError | None = None
        interrupted = False
        result = run_meta = proposal = None
        assessment, assess_meta = None, {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            classify_future = submit(pool, run_classify) if ctx.profile.classify and not reuse_cls else None
            assess_future = submit(pool, run_assess) if ctx.profile.assess and not reuse_assess else None
            motivation_future = None if reuse_motivation or not ctx.profile.motivation else submit(pool,
                ensure_motivation, self.config, self.db, self.backend, meta.to_dict(),
                force=ctx.force, retry=True, fingerprint=fp)
            if classify_future is not None:
                try:
                    result, run_meta, proposal = classify_future.result()
                except RunInterrupted:
                    interrupted = True
                except ModelError as exc:
                    classify_error = exc
            if assess_future is not None:
                try:
                    assessment, assess_meta = assess_future.result()
                except RunInterrupted:
                    interrupted = True
                except Exception as exc:  # noqa: BLE001 - assess() absorbs ModelError; be safe
                    assessment, assess_meta = None, {"error": str(exc)[:200]}
            if motivation_future is not None:
                try:
                    motivation_future.result()
                except RunInterrupted:
                    interrupted = True
                except Exception as exc:  # optional; ensure_motivation already records the failed run
                    self._emit(progress, paper_id, "classifying",
                               {"motivation_warning": str(exc)[:200]})

        if assessment:
            assessment["doc_fingerprint"] = fp
            assessment["run_id"] = self._record_run(
                paper_id=paper_id, task="assess", meta=assess_meta
            )
            atomic_write_json(base / "assessment.json", assessment)
        elif assess_meta:
            # Optional stage, but a failed call is still a paid call.
            self._record_run(paper_id=paper_id, task="assess", status="failed",
                             error_code=str(assess_meta.pop("error", ""))[:200],
                             meta=assess_meta)

        if classify_error is not None:
            self._record_run(paper_id=paper_id, task="classify",
                             status="truncated" if isinstance(classify_error, ModelTruncated) else "failed",
                             error_code=(classify_error.code or "model_error")[:200],
                             meta=_reply_meta(classify_error))
            raise PipelineError("classify_failed", str(classify_error)) from classify_error
        if result is None:
            if interrupted:
                raise RunInterrupted()
            return  # classification was reused; optional tasks were filled

        # Database first, artefact second: the artefact is the resume marker,
        # so it must never exist without the rows it stands for.
        from carrel.classify.migration import project_classification
        with taxonomy_lock:
            current = Taxonomy.layered(Path(__file__).parent / 'taxonomy.toml', self.data_dir / 'taxonomy.toml')
            classification = project_classification(result.to_dict(), current.renames)
            self.db.replace_tags(paper_id, [t.to_dict() for t in result.tags])
            self.db.execute(
                "UPDATE papers SET discipline=?, field=?, updated_at=? WHERE id=?",
                (classification['discipline'], classification['field'], _now(), paper_id),
            )
            atomic_write_json(base / "classification.json",
                              {**classification, "doc_fingerprint": fp})
        self._record_run(paper_id=paper_id, task="classify", meta=run_meta)
        if proposal:
            self.db.add_proposal(
                pid=f"prop-{uuid.uuid4().hex[:10]}",
                kind="discipline",
                parent="",
                label=str(proposal.get("discipline") or "未分类"),
                paper_id=paper_id,
                reason=str(proposal.get("reason") or ""),
            )
        if interrupted:
            raise RunInterrupted()

    # -------------------------------------------------------------- stage 5
    def _stage_relate(self, paper_id: str, meta: PaperMeta,
                      progress: ProgressFn | None) -> list[str]:
        base = paper_dir(self.data_dir, paper_id)
        self.db.set_status(paper_id, "relating")
        self._emit(progress, paper_id, "relating")

        try:
            bundle = self.relations.collect(
                paper_id=paper_id,
                title=meta.title,
                arxiv_id=_arxiv_of(paper_id),
                doi="",
                abstract=meta.abstract,
            )
        except Exception as exc:  # noqa: BLE001 - relations must never block reading
            atomic_write_json(base / "relations.json",
                              {"coverage": {}, "errors": {"collect": str(exc)}})
            return [f"relations: {exc}"]

        known = self._known_identifiers(paper_id)
        link_to_library(paper_id, bundle, known)
        for kind in ("cites", "cited_by", "similar"):
            rows = [r.to_dict() for r in getattr(bundle, kind)]
            self.db.replace_relations(paper_id, kind, rows)
        from carrel.motivation.extract import document_fingerprint
        atomic_write_json(base / "relations.json", {**bundle.to_dict(), 'doc_fingerprint': document_fingerprint(base)})

        if not self.defer_library:
            self._backfill_reverse_edges(paper_id, bundle)
        warnings = [f"{k}: {v}" for k, v in bundle.errors.items()]
        for kind, cov in bundle.coverage.items():
            if cov == "failed":
                warnings.append(f"{kind} unavailable")
        return warnings

    def _known_identifiers(self, exclude: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in self.db.query("SELECT id FROM papers ORDER BY id"):
            pid = row["id"]
            if pid == exclude:
                continue
            if pid.startswith("arxiv:"):
                out[pid.split(":", 1)[1].lower()] = pid
            elif pid.startswith("doi:"):
                out[pid.split(":", 1)[1].lower()] = pid
        return out

    def _backfill_reverse_edges(self, paper_id: str, bundle) -> None:
        from carrel.relate.relink import relink_library
        relink_library(self.db)


# ------------------------------------------------------------------ helpers

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_list(value: Any) -> list[str]:
    """``authors`` is stored as a JSON string in SQLite; accept both shapes."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _trace_of(reply: Any) -> dict[str, Any]:
    """Payload snapshot of a model call, for the run's detail page."""
    return {
        "system": getattr(reply, "system_prompt", "") or "",
        "user": getattr(reply, "user_prompt", "") or "",
        "response": getattr(reply, "raw", None) or {},
        "text": getattr(reply, "text", "") or "",
    }


def _arxiv_of(paper_id: str) -> str:
    return paper_id.split(":", 1)[1] if paper_id.startswith("arxiv:") else ""


#: A single translation request should stay well under the model's context and
#: finish in a reasonable time; longer sections are sliced.
MAX_TRANSLATE_CHARS = 6000


def _split_for_translation(blocks: list[dict[str, Any]],
                           limit: int = MAX_TRANSLATE_CHARS) -> list[list[dict[str, Any]]]:
    """Split a section into request-sized slices, never mid-block."""
    slices: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for block in blocks:
        length = len(str(block.get("text") or ""))
        if current and size + length > limit:
            slices.append(current)
            current, size = [], 0
        current.append(block)
        size += length
    if current:
        slices.append(current)
    return slices or [blocks]


def _module_key(section: Section, jobs: list[tuple[Section, Any]]) -> str:
    """File stem for a section's extract: ``kind`` for the first section of a
    kind, ``kind-<section id>`` for the rest.

    Derived from the planned job list rather than from completion order, so a
    resumed run computes the same key for the same section as the run that
    wrote the artefact (the old code keyed on which future finished first).
    """
    first = next((s for s, _ in jobs if s.kind == section.kind), None)
    if first is None or first.id == section.id:
        return section.kind
    return f"{section.kind}-{section.id}"


def _reply_meta(exc: BaseException) -> dict[str, Any] | None:
    """Accounting carried by a failed call, if the exception kept its reply.

    ``ModelTruncated`` and ``ModelSchemaError`` carry the ``ModelReply`` that
    was paid for; a transport error carries nothing. Recording the former with
    an empty meta was how truncated translations booked as zero tokens.
    """
    reply = getattr(exc, "reply", None)
    return reply.accounting() if reply is not None else None


def _extracts_fingerprint(extracts: dict[str, ModuleExtract]) -> str:
    """Identity of the extract set a summary was built from."""
    h = hashlib.sha256()
    for key in sorted(extracts):
        ex = extracts[key]
        h.update(f"{key}\x1f{ex.kind}\x1f{ex.digest}\n".encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:16]


def _prune_sections(folder, sections, *, translation):
    """Prune only chapters removed from the document, regardless of this run's scope."""
    rows = [s.to_dict() if hasattr(s, 'to_dict') else s for s in sections]
    keep = set()
    for path in folder.glob('*.json'):
        part = read_json(path, {}) or {}
        if not isinstance(part, dict):
            continue
        title = part.get('title' if translation else 'title_original', '')
        if any(s['kind'] == part.get('kind') and s.get('title', '').strip() == title.strip()
               and (translation or (s.get('block_ids') and part.get('block_span') ==
                                    [s['block_ids'][0], s['block_ids'][-1]])) for s in rows):
            keep.add(path.stem)
    return _prune_json(folder, keep=keep)


def _prune_json(folder: Path, *, keep: set[str]) -> list[str]:
    """Delete ``<stem>.json`` files whose stem is not in ``keep``.

    Names starting with ``_`` (``_term_review.json``) are never touched. Stage
    artefacts for a section the document no longer has would otherwise be read
    by later stages and rendered in the reader as a chapter that isn't there.
    """
    removed: list[str] = []
    if not folder.is_dir():
        return removed
    for path in folder.glob("*.json"):
        if path.name.startswith("_") or path.stem in keep:
            continue
        try:
            path.unlink()
            removed.append(path.stem)
        except OSError:
            pass
    return removed


def _load_extracts(base: Path, *, fingerprint: str | None = None) -> dict[str, ModuleExtract]:
    """Extracts on disk; with ``fingerprint`` only those written from that doc."""
    out: dict[str, ModuleExtract] = {}
    folder = base / "modules"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        data = read_json(path, None)
        if not isinstance(data, dict):
            continue
        if fingerprint is not None and data.get("doc_fingerprint") != fingerprint:
            continue
        if fingerprint is not None and not (data.get("digest") or data.get("points")):
            continue  # an empty extract is not worth keeping over a fresh call
        out[path.stem] = ModuleExtract(**{
            k: data.get(k, v) for k, v in {
                "kind": "", "title_original": "", "block_span": [], "digest": "",
                "points": [], "definitions": [], "formulas": [], "numbers": [],
                "open_questions": [],
            }.items()
        })
    return out


def _load_summary(base: Path) -> Summary:
    data = read_json(base / "summary.json", {}) or {}
    return Summary(**{k: data.get(k, v) for k, v in {
        "one_line": "", "one_line_source": "model", "tldr": "", "contributions": [],
        "section_digests": [], "argument_chain": [], "limitations": [],
        "reading_guide": {}, "scope": "full",
    }.items()})


def _derive_title(doc: DocModel, extracts: dict[str, ModuleExtract]) -> str:
    """Find the paper's real title in the document front matter.

    MinerU marks headings with ``text_level``; the paper title is the first
    level-1 heading on page 1 and appears before any section heading. Section
    headings ("Abstract", "3 Method") must never be promoted to the paper title.
    """
    page_one = [b for b in doc.blocks if b.page == 1]
    for block in page_one:
        if block.type == "title" and (block.level or 9) <= 1:
            text = block.text.strip()
            # A real title is longer than a section label and not a known heading.
            if len(text) >= 12 and classify_heading(text) == "unknown":
                return text
    # Fall back to any level-1 heading on the first two pages.
    for block in doc.blocks:
        if block.page > 2:
            break
        if block.type == "title" and (block.level or 9) <= 1:
            text = block.text.strip()
            if len(text) >= 12 and classify_heading(text) == "unknown":
                return text
    return ""


def _extract_abstract(doc: DocModel, sections: list[dict[str, Any]]) -> str:
    """Read the abstract from the source text, never from a model summary."""
    by_id = doc.blocks_by_id()
    for section in sections:
        if section.get("kind") != "abstract":
            continue
        parts = []
        for bid in section.get("block_ids", []):
            block = by_id.get(bid)
            if block is None or block.type == "title":
                continue
            parts.append(block.text.strip())
        text = " ".join(p for p in parts if p).strip()
        # Strip a leading "Abstract" word if MinerU folded it into the body.
        if text.lower().startswith("abstract"):
            text = text[len("abstract"):].lstrip(" :—-")
        return text[:4000]

    # MinerU often fuses the abstract into a front-matter body block on page one
    # ("Abstract We study ..."), so no `abstract` section exists at all. Fall back
    # to the first page-one text block that starts with the word.
    for block in doc.blocks:
        if block.page > 1:
            break
        if block.type in ("title", "formula", "table", "figure"):
            continue
        stripped = block.text.strip()
        if re.match(r"^abstract\b[\s:—-]", stripped, re.I):
            return re.sub(r"^abstract\b[\s:—-]*", "", stripped, flags=re.I)[:4000]
    return ""


def _fill_meta_from_extracts(
    meta: PaperMeta,
    doc: DocModel | None,
    sections: list[dict[str, Any]] | None,
    extracts: dict[str, ModuleExtract],
    summary: Summary,
) -> None:
    """Fill only factual fields, and only from source text.

    The summary's ``one_line`` is a model judgement (``origin=llm``) and must not
    be written into the ``abstract`` column, which is presented as source text.
    """
    if not meta.title and doc is not None:
        meta.title = _derive_title(doc, extracts)
    if not meta.abstract and doc is not None:
        meta.abstract = _extract_abstract(doc, sections or [])


def _context(meta: PaperMeta, previous_tail: str) -> str:
    bits = []
    if meta.title:
        bits.append(f"标题：{meta.title}")
    if previous_tail:
        bits.append("上一节末尾：" + previous_tail[:600])
    return "\n".join(bits)


def _tail_text(blocks: list[dict[str, Any]]) -> str:
    for block in reversed(blocks):
        if block.get("type") not in SKIP_TYPES:
            return str(block.get("text", ""))[-400:]
    return ""
