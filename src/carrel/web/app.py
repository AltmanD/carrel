"""FastAPI application: read-only browsing plus import and job control."""

from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from carrel.runtime.locks import taxonomy_lock
from carrel.titles import project_title, ensure_title, save_human_title
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from carrel.classify.taxonomy import CATEGORY_KEYS, Taxonomy, matches_category, project_path
from carrel.config import AppConfig, MODEL_TASKS, DEFAULT_MAX_TOKENS
from carrel.model.router import ProviderRouter
from carrel.model.verification import probe_provider, verify_provider, verification_status, recommended_routes
from carrel.providers import ProviderConfig, ProviderRegistry, RouteConfig, THINKING_OPTIONS, secret_header
from carrel.graph.build import (
    DEFAULT_CAPACITY,
    DEFAULT_SINGLE_MAX_PAPERS,
    READABLE_STATUSES,
    SINGLE_MAX_OPTIONS,
)
from carrel.graph.links import relation_link
from carrel.graph.service import GraphService
from carrel.store.db import Database
from carrel.store.paths import atomic_write_json, atomic_write_text, paper_dir, read_json
from carrel.web.imports import preview_imports
from carrel.profile import PRESETS, parse_profile, stored_profile, default_profile, save_default, estimate, task_estimates
from carrel.coverage import paper_coverage, missing_capabilities
from carrel.graph.requirements import readiness, CAPABILITY_LABELS

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _authors_display(raw: Any) -> str:
    if isinstance(raw, str):
        try:
            authors = json.loads(raw)
        except (ValueError, TypeError):
            return raw
    else:
        authors = raw
    if not isinstance(authors, list):
        return ""
    names = [str(a.get("name", "") if isinstance(a, dict) else a) for a in authors]
    names = [name for name in names if name]
    return ", ".join(names[:2]) + (" 等" if len(names) > 2 else "")


def create_app(config: AppConfig, db: Database) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.jobs.scheduler.recover()
        app.state.graphs.refresh()
        yield
        import asyncio
        await asyncio.to_thread(app.state.jobs.scheduler.shutdown)
        await asyncio.to_thread(app.state.graphs.close)
    app = FastAPI(title="Carrel", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # Cache-busting stamp for static assets: without it the browser keeps an
    # old app.js and every behaviour change looks "not implemented" until a
    # hard refresh. The stamp changes whenever any static file changes.
    static_v = max(
        (p.stat().st_mtime_ns for p in STATIC_DIR.rglob('*') if p.is_file()),
        default=0,
    )
    templates.env.globals["static_v"] = static_v
    templates.env.globals["status_labels"] = {
        "ready": "可阅读", "ready_with_warning": "关联待补全", "partial": "翻译待补全",
        "failed": "处理失败", "queued": "等待处理", "parsing": "解析中",
        "structuring": "整理中", "translating": "翻译中", "classifying": "分类中",
        "relating": "关联中", "running": "处理中", "succeeded": "已完成",
        "indexed": "已收录 · 未处理",
        "retry_wait": "等待重试",
    }
    templates.env.globals["stage_labels"] = {
        "parse": "解析", "structure": "整理", "translate": "翻译",
        "classify": "分类", "relate": "关联", "pipeline": "全文处理",
        "parsing": "解析", "structuring": "整理", "translating": "翻译",
        "classifying": "分类", "relating": "关联",
        "motivation_tree": "动机脉络",
        "title": "中文标题", "preview": "论文速览",
    }
    app.state.config = config
    app.state.db = db
    app.state.graphs = GraphService(db, config.data_dir)
    router = ProviderRouter.from_config(config, caps_path=config.data_dir / "model_caps.json")
    app.state.router = router
    app.state.jobs = _JobRunner(config, db, backend=router)
    settings_lock = threading.RLock()
    registry = ProviderRegistry(deepcopy(config.providers), deepcopy(config.task_routes),
                                config.default_provider, config.provider_file or config.data_dir / "providers.toml")
    verification_jobs: dict[str, dict] = {}
    app.mount("/static", _static_files(), name="static")
    from carrel.web.local_sync import register_local_sync

    register_local_sync(app, config, db, templates, _settings_csrf, _err)

    def paper_categories(paper):
        classification = read_json(paper_dir(config.data_dir, paper["id"]) / "classification.json", {}) or {}
        raw = classification.get("categories") or [{
            "discipline": paper.get("discipline", ""), "field": paper.get("field", ""),
        }]
        renames = Taxonomy.load(config.data_dir / 'taxonomy.toml', fallback=False).renames
        paths = [project_path(path, renames) for path in raw if isinstance(path, dict)]
        primary = classification.get('primary_category_id')
        return sorted(paths, key=lambda path: path.get('category_id') != primary) if primary else paths

    def library_papers(*, discipline="", field="", subfield="", **filters):
        papers = [project_title(p, paper_dir(config.data_dir, p['id'])) for p in db.list_papers(**filters)]
        if filters.get('sort') == 'citations':
            citation_metrics(papers)
            papers.sort(key=lambda p: (-p['cited_by_count'], p.get('title') or '', p['id']))
        if not discipline and not field and not subfield:
            return papers
        if subfield:
            mapped = project_path({'discipline': discipline, 'field': field, 'subfield': subfield})
            discipline, field = mapped['discipline'], mapped['field']
        return [paper for paper in papers if any(
            (not discipline or category.get("discipline") == discipline)
            and (not field or category.get("field") == field)
            for category in paper_categories(paper)
        )]

    def citation_metrics(papers):
        counts = {r['src_id']: r['n'] for r in db.query(
            "SELECT src_id, COUNT(DISTINCT dst_id) AS n FROM relations "
            "WHERE kind='cited_by' AND src_id!=dst_id GROUP BY src_id")}
        for paper in papers:
            relations = read_json(paper_dir(config.data_dir, paper['id']) / 'relations.json', {}) or {}
            count = relations.get('cited_by_count') if isinstance(relations, dict) else None
            known = isinstance(count, int) and not isinstance(count, bool) and count >= 0
            paper['citation_total_known'] = known
            paper['cited_by_count'] = count if known else counts.get(paper['id'], 0)

    def used_categories(*, include_custom=False):
        # A paper can belong to several paths; count each paper once per facet.
        groups = {}
        entries = [(paper["id"], category) for paper in db.list_papers()
                   for category in paper_categories(paper)]
        if include_custom:
            entries.extend((None, path) for path in _custom_paths(config))
        for paper_id, category in entries:
            disc, field = (category.get(key) for key in ('discipline', 'field'))
            if not disc or disc == "未分类":
                continue
            group = groups.setdefault(disc, {"ids": set(), "fields": {}})
            if paper_id:
                group["ids"].add(paper_id)
            if field:
                leaf = group["fields"].setdefault(field, {"ids": set()})
                if paper_id:
                    leaf["ids"].add(paper_id)
        return [{"path": disc, "label": disc, "count": len(group["ids"]),
                 "children": [{"path": field, "label": field, "count": len(leaf["ids"]),
                               "children": []}
                              for field, leaf in sorted(group["fields"].items())]}
                for disc, group in sorted(groups.items())]

    # ------------------------------------------------------------- pages
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html", {})

    def save_registry(updated):
        # Validate/build before saving; the existing router identity survives updates.
        prepared = ProviderRouter(updated.providers, updated.routes, fallback_config=config.model,
                                  caps_path=config.data_dir / "model_caps.json",
                                  default_provider=updated.default_provider)
        updated.save()
        router.adopt(prepared)
        registry.providers, registry.routes = updated.providers, updated.routes
        registry.default_provider = updated.default_provider
        config.providers, config.task_routes = deepcopy(updated.providers), deepcopy(updated.routes)
        config.default_provider = updated.default_provider
        config.provider_error = ""

    @app.get("/api/providers")
    def api_providers():
        with settings_lock:
            return {"providers": registry.public_providers(), "default_provider": registry.default_provider,
                    "warning": getattr(config, "provider_error", "")}

    @app.post("/api/providers")
    async def api_provider_save(request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        body = await _json_body(request)
        with settings_lock:
            try:
                pid = body.get("id", "")
                if not isinstance(pid, str):
                    raise ValueError("ID 须为文本")
                pid = pid.strip()
                old = registry.providers.get(pid)
                data = old.to_dict() if old else {"id": pid}
                allowed = set(ProviderConfig.__dataclass_fields__)
                if set(body) - allowed:
                    raise ValueError("包含未知配置字段")
                data.update(body)
                for name in ("id", "label", "protocol", "base_url", "api_key", "api_key_env", "notes", "model", "thinking"):
                    if name in data and not isinstance(data[name], str):
                        raise ValueError(f"{name} 须为文本")
                if old and (not str(body.get("api_key", "")).strip() or body.get("api_key") == old.masked_api_key):
                    data["api_key"] = old.api_key
                for name in ("extra_headers", "pricing"):
                    if name in data and not isinstance(data[name], dict):
                        raise ValueError(f"{name} 须为 JSON 对象")
                for name in ("timeout_s", "max_retries", "max_concurrency", "min_concurrency"):
                    if name in data and (not isinstance(data[name], int) or isinstance(data[name], bool)):
                        raise ValueError(f"{name} 须为整数")
                if old:
                    public_headers = old.public_dict()["extra_headers"]
                    data["extra_headers"] = {k: old.extra_headers[k] if secret_header(k) and k in old.extra_headers and v == public_headers[k] else v
                                             for k, v in data.get("extra_headers", {}).items()}
                provider = ProviderConfig(**data)
                provider.validate()
                updated = deepcopy(registry)
                updated.providers[provider.id] = provider
                save_registry(updated)
            except (TypeError, ValueError) as exc:
                return _err(422, "invalid_provider", str(exc))
            except OSError:
                return _err(503, "settings_write_failed", "设置文件写入失败，请检查资料库目录权限；运行配置未修改")
            return {"provider": provider.public_dict(), "message": "已保存，后续模型调用立即使用新配置"}

    @app.delete("/api/providers/{provider_id}")
    def api_provider_delete(provider_id: str, request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        with settings_lock:
            if provider_id not in registry.providers:
                return _err(404, "not_found", "提供方不存在")
            references = [t for t, r in registry.routes.items() if r.provider == provider_id]
            if registry.default_provider == provider_id:
                references.insert(0, "默认提供方")
            if references:
                return _err(409, "provider_in_use", "该提供方仍被使用：" + "、".join(references), "请先修改默认提供方和对应任务分配")
            if verification_jobs.get(provider_id, {}).get("status") == "running":
                return _err(409, "verification_running", "该提供方正在验证，请完成后再删除")
            updated = deepcopy(registry)
            del updated.providers[provider_id]
            try:
                save_registry(updated)
            except OSError:
                return _err(503, "settings_write_failed", "设置文件写入失败，请检查资料库目录权限；运行配置未修改")
            return {"ok": True}

    @app.post("/api/providers/{provider_id}/test")
    def api_provider_test(provider_id: str, request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        with settings_lock:
            provider = deepcopy(registry.providers.get(provider_id))
        if provider is None:
            return _err(404, "not_found", "提供方不存在")
        return probe_provider(provider, config.model)

    @app.get("/api/providers/{provider_id}/verification")
    def api_provider_verification(provider_id: str):
        with settings_lock:
            provider = registry.providers.get(provider_id)
            if provider is None:
                return _err(404, "not_found", "提供方不存在")
            return {**verification_status(config.data_dir, provider),
                    "job": dict(verification_jobs.get(provider_id, {}))}

    @app.post("/api/providers/{provider_id}/verify")
    async def api_provider_verify(provider_id: str, request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        body = await _json_body(request)
        with settings_lock:
            provider = deepcopy(registry.providers.get(provider_id))
            if provider is None:
                return _err(404, "not_found", "提供方不存在")
            if not provider.configured or not provider.model:
                return _err(422, "provider_unconfigured", "请先配置 API 地址、密钥和模型名")
            baseline_id = str(body.get("baseline_provider") or "")
            baseline = deepcopy(registry.providers.get(baseline_id)) if baseline_id else None
            if baseline_id and (baseline is None or not baseline.configured or not baseline.model.lower().startswith("glm")):
                return _err(422, "invalid_baseline", "请选择已配置密钥与模型名的 GLM 提供方作为基线")
            if not baseline_id and (not config.model.api_key or any(not config.model.for_task(t)[0].lower().startswith("glm") for t in MODEL_TASKS)):
                return _err(422, "invalid_baseline", "原配置不是可用的 GLM 基线，请添加并选择一个 GLM 提供方")
            if verification_jobs.get(provider_id, {}).get("status") == "running":
                return _err(409, "verification_running", "验证正在进行")
            job = {"status": "running", "done": 0, "total": len(MODEL_TASKS), "task": ""}
            verification_jobs[provider_id] = job
            def run():
                def progress(task, done, total):
                    with settings_lock:
                        job.update(task=task, done=done, total=total)
                try:
                    report = verify_provider(config, provider, paper_id=str(body.get("paper_id", "")), baseline_provider=baseline, progress=progress)
                    with settings_lock:
                        job.update(status="succeeded" if report["passed"] else "failed")
                except Exception as exc:
                    with settings_lock:
                        job.update(status="failed", error=provider.redact(str(exc))[:2000])
            threading.Thread(target=run, daemon=True, name="provider-verification").start()
            return JSONResponse(status_code=202, content={"job": dict(job)})

    @app.get("/api/task-routes")
    def api_task_routes():
        with settings_lock:
            return {"routes": registry.route_dict(), "tasks": list(MODEL_TASKS),
                    "default_provider": registry.default_provider, "providers": registry.public_providers(),
                    "defaults": {t: {"name": config.model.for_task(t)[0], "thinking": config.model.for_task(t)[1],
                                     "max_tokens": config.model.for_task(t)[2]} for t in MODEL_TASKS},
                    "default_max_tokens": DEFAULT_MAX_TOKENS,
                    "legacy": {"name": config.model.name, "thinking": config.model.thinking},
                    "verification": {p.id: {k: v for k, v in verification_status(config.data_dir, p).items() if k != "report"}
                                     for p in registry.providers.values()}}

    @app.post("/api/task-routes")
    async def api_task_routes_save(request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        body = await _json_body(request)
        with settings_lock:
            updated = deepcopy(registry)
            if "recommend" in body:
                provider = registry.providers.get(str(body["recommend"]))
                if provider is None:
                    return _err(404, "not_found", "提供方不存在")
                if not verification_status(config.data_dir, provider)["passed"] or verification_jobs.get(provider.id, {}).get("status") == "running":
                    return _err(409, "verification_required", "当前配置尚未通过全部任务与 GLM 基线验证", "先完成逐任务验证，再应用推荐路由")
                updated.default_provider = provider.id
                updated.routes = recommended_routes(provider)
            else:
                try:
                    if not isinstance(body.get("routes"), dict):
                        raise ValueError("routes 须为任务配置对象")
                    default = body.get("default_provider", registry.default_provider)
                    if not isinstance(default, str) or (default and default not in registry.providers):
                        raise ValueError("默认提供方不存在")
                    updated.default_provider = default
                    updated.routes = {}
                    for task, value in body["routes"].items():
                        if task not in MODEL_TASKS or not isinstance(value, dict) or set(value) - {"provider", "name", "thinking", "max_tokens"}:
                            raise ValueError("任务名或配置字段无效")
                        route = RouteConfig(**value)
                        if any(not isinstance(v, str) for v in (route.provider, route.name, route.thinking)):
                            raise ValueError("提供方、模型和思考强度须为文本")
                        if route.thinking not in THINKING_OPTIONS or not isinstance(route.max_tokens, int) or isinstance(route.max_tokens, bool) or not 0 <= route.max_tokens <= 1000000:
                            raise ValueError("思考强度或输出上限无效")
                        if route.provider and route.provider not in registry.providers:
                            raise ValueError("任务引用了不存在的提供方")
                        updated.routes[task] = route
                    used = {r.provider for r in updated.routes.values() if r.provider} | ({default} if default else set())
                    if any(not registry.providers[pid].configured or not registry.providers[pid].model for pid in used):
                        raise ValueError("被使用的提供方必须配置地址、密钥和模型名")
                except (TypeError, ValueError) as exc:
                    return _err(422, "invalid_routes", str(exc))
            try:
                save_registry(updated)
            except OSError:
                return _err(503, "settings_write_failed", "设置文件写入失败，请检查资料库目录权限；运行配置未修改")
            return {"ok": True, "routes": registry.route_dict(), "default_provider": registry.default_provider}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        q = request.query_params.get("q", "")
        discipline = request.query_params.get("discipline", "")
        field_name = request.query_params.get("field", "")
        subfield = request.query_params.get("subfield", "")
        tag = request.query_params.get("tag", "")
        collection = request.query_params.get("collection", "")
        status = request.query_params.get("status", "")
        sort = request.query_params.get("sort", "recent")
        if sort not in ('recent', 'title', 'year', 'rank', 'citations'):
            sort = 'recent'
        facet = request.query_params.get('facet', '')
        if facet not in ('collection', 'category', 'keyword'):
            facet = 'keyword' if tag else 'category' if discipline or field_name or subfield else 'collection'
        filters = {"q": q, "discipline": discipline, "field": field_name, "subfield": subfield,
                   "tag": tag, "status": status, "sort": sort, "collection": collection, 'facet': facet}

        def filter_url(**changes):
            values = {**filters, **changes}
            return "/?" + urlencode({k: v for k, v in values.items() if v})

        papers = library_papers(q=q, discipline=discipline, field=field_name,
                                subfield=subfield, tag=tag, status=status, sort=sort, collection=collection)
        if sort != 'citations':
            citation_metrics(papers)
        tags = db.all_tags()
        for p in papers:
            paper_tags = db.tags_for(p["id"])
            p["tags"] = paper_tags[:4]
            p["tag_count"] = len(paper_tags)
            p["human_priorities"] = sorted({n["priority"] for n in db.notes_for(p["id"])
                                            if n["priority"] and (not collection or n["collection_id"] == collection)})
            p["authors_display"] = _authors_display(p.get("authors"))
            pdir = paper_dir(config.data_dir, p["id"])
            p["has_pdf"] = (pdir / "source.pdf").is_file()
            summary = read_json(pdir / "summary.json", {}) or {}
            # The preview lands minutes before the full summary; fall back to it
            # so the card is readable as soon as the preview call finishes.
            preview = read_json(pdir / "preview.json", {}) or {}
            for key in ("one_line", "tldr"):
                if not summary.get(key) and preview.get(key):
                    summary[key] = preview[key]
            p["summary"] = summary
        collections = db.collections()
        visible_collections = [c for c in collections if c['name'].strip() != '未归类']
        return templates.TemplateResponse(request, "index.html", {
            "papers": papers,
            "categories": used_categories(),
            "tags": tags,
            "collections": visible_collections,
            "collection_names": {c["id"]: c["name"] for c in collections},
            "filters": filters,
            "filter_url": filter_url,
            "has_filters": any((q, discipline, field_name, subfield, tag, status, collection)),
            "total": db.one("SELECT COUNT(*) AS n FROM papers")["n"],
            "tag_total": len(tags),
            "health": _health(config),
            # Every path in the taxonomy for the facet's free-input datalist.
            "all_categories": sorted(
                {p["discipline"] for p in _taxonomy_paths(config)} |
                {p["field"] for p in _taxonomy_paths(config) if p["field"]}
            ),
        })

    @app.get("/categories", response_class=HTMLResponse)
    def categories(request: Request):
        return templates.TemplateResponse(request, "categories.html", {
            "tree": used_categories(include_custom=True),
            "proposals": db.list_proposals(),
            "unclassified": [p for p in db.list_papers() if not p.get("discipline")
                             or p.get("discipline") == "未分类"],
            "taxonomy_paths": _taxonomy_paths(config),
        })

    @app.get("/papers/{paper_id:path}", response_class=HTMLResponse)
    def paper_detail(request: Request, paper_id: str):
        row = db.get_paper(paper_id)
        if not row:
            raise HTTPException(404, detail={"error_code": "not_found",
                                             "message": f"paper {paper_id} not found"})
        base = paper_dir(config.data_dir, paper_id)
        row = project_title(row, base)
        doc = read_json(base / "doc.json", {}) or {}
        sections = read_json(base / "sections.json", {}) or {}
        summary = read_json(base / "summary.json", {}) or {}
        # Preview fields stand in until the full summary exists, so the reading
        # entry is usable minutes before the summarize call has run. When the
        # summary's headline is a stitched fallback, the preview's model-written
        # one reads better, so it wins.
        preview = read_json(base / "preview.json", {}) or {}
        for key in ("one_line", "tldr"):
            if not summary.get(key) and preview.get(key):
                summary[key] = preview[key]
        if summary.get("one_line_source") == "fallback" and preview.get("one_line"):
            summary["one_line"] = preview["one_line"]
        if preview.get("contributions") and not summary.get("contributions"):
            summary["contributions"] = preview["contributions"]
        classification = read_json(base / "classification.json", {}) or {}
        classification['categories'] = paper_categories(row)
        # Manual edits live in SQLite; the model artifact is not the tag source.
        classification["tags"] = db.tags_for(paper_id)
        row["authors_display"] = _authors_display(row.get("authors"))
        assessment = read_json(base / "assessment.json", {}) or {}
        relations = read_json(base / "relations.json", {}) or {}

        # Precompute a display link for every relation row: the local library
        # first, then arXiv, DOI, and finally the OpenAlex work page — a bare
        # `openalex:W...` id is unreadable and unclickable.
        def _relation_link(r: dict[str, Any]) -> dict[str, Any]:
            return relation_link(r, local_lookup=db.get_paper)

        for kind in ("cites", "cited_by", "similar"):
            relations[kind] = [_relation_link(r) for r in (relations.get(kind) or [])]
        glossary = read_json(base / "glossary.json", {}) or {}

        modules: dict[str, Any] = {}
        mod_dir = base / "modules"
        if mod_dir.is_dir():
            for path in sorted(mod_dir.glob("*.json")):
                modules[path.stem] = read_json(path, {}) or {}

        translations: dict[str, Any] = {}
        tr_dir = base / "translation"
        if tr_dir.is_dir():
            for path in sorted(tr_dir.glob("*.json")):
                if path.name.startswith("_"):
                    continue
                translations[path.stem] = read_json(path, {}) or {}

        blocks_by_id = {b["id"]: b for b in doc.get("blocks", [])}

        # Reading order comes from the paper's own section sequence, not the
        # file names: module keys are derived from section kinds, so match each
        # artifact back to its section by title and sort by that.
        section_list = sections.get("sections", [])
        order_by_title: dict[str, int] = {}
        for idx, sdata in enumerate(section_list):
            order_by_title.setdefault((sdata.get("title") or "").strip(), idx)

        def _order(item: tuple[str, dict]) -> tuple:
            key, payload = item
            title = (payload.get("title_original") or payload.get("title") or "").strip()
            return (order_by_title.get(title, 999), key)

        ordered_modules = sorted(modules.items(), key=_order)

        # Translation rendered as a readable article: one entry per module in
        # reading order. Consecutive text blocks merge into one flowing
        # paragraph — block boundaries are layout artifacts of the PDF, not
        # paragraph boundaries, and rendering each block separately breaks
        # sentences mid-line. Every paragraph also carries its source text and
        # block ids so the reading pane can show the original beside it.
        def _joinable(t: str) -> bool:
            return not t or ("一" <= t[-1] <= "鿿")

        def _merge(parts_in: list[str]) -> tuple[str, list[str]]:
            text = ""
            parts_out = []
            for part in parts_in:
                part = part if _joinable(text) else " " + part
                parts_out.append(part)
                text += part
            return text.strip(), parts_out

        article = []
        for key, tr in sorted(translations.items(), key=_order):
            trans_map = tr.get("translations", {}) if isinstance(tr, dict) else {}
            if not trans_map:
                continue
            needs = set(tr.get("needs_review", []) if isinstance(tr, dict) else [])
            sec = next((s for s in section_list
                        if (s.get("title") or "").strip()
                        == (tr.get("title") or "").strip()), None)
            paragraphs: list[dict[str, Any]] = []
            buf: list[str] = []
            buf_src: list[str] = []
            buf_ids: list[str] = []

            def flush() -> None:
                if not buf:
                    return
                text, parts = _merge(buf)
                source, src_parts = _merge(buf_src)
                pages = []
                for block_id in buf_ids:
                    page = blocks_by_id.get(block_id, {}).get("page")
                    if page and page not in pages:
                        pages.append(page)
                paragraphs.append({
                    "id": buf_ids[0],
                    "type": "text",
                    "page": pages[0] if pages else None,
                    "pages": pages,
                    "text": text,
                    "parts": parts,
                    "source": source,
                    # The source column always needs something to show; when a
                    # block had no original text the translation stands in.
                    "src_parts": src_parts or parts,
                    "block_ids": list(buf_ids),
                    "untranslated": False,
                    "needs_review": any(b in needs for b in buf_ids),
                })
                buf.clear()
                buf_src.clear()
                buf_ids.clear()

            source_ids = sec.get("block_ids", []) if sec else sorted(trans_map)
            for bid in source_ids:
                block = blocks_by_id.get(bid) or {}
                btype = block.get("type") or "text"
                src = str(block.get("text") or "").strip()
                dst = str(trans_map.get(bid) or "").strip()
                if btype == "title":
                    continue  # the section header already shows above
                if btype in ("formula", "caption", "table", "figure"):
                    # A formula keeps the source markup (KaTeX renders it once);
                    # captions and tables show their translation beside the
                    # original, and never disappear just because it is missing.
                    text = src if btype == "formula" else (dst or src)
                    if not text:
                        continue
                    flush()
                    page = block.get("page")
                    paragraphs.append({
                        "id": bid,
                        "type": btype,
                        "page": page,
                        "pages": [page] if page else [],
                        "text": text,
                        "parts": [text],
                        "source": src or text,
                        "src_parts": [src or text],
                        "block_ids": [bid],
                        "untranslated": btype != "formula" and not dst,
                        "needs_review": bid in needs,
                    })
                    continue
                if not dst:
                    # Body blocks without a translation would turn the
                    # comparison column into a wall of English; skip them and
                    # let the section's coverage be visible elsewhere.
                    continue
                previous_page = blocks_by_id.get(buf_ids[-1], {}).get("page") if buf_ids else None
                page = block.get("page")
                if buf and (len("".join(buf)) > 900
                            or (page and previous_page and page != previous_page)):
                    flush()  # A floating page must not contain text from the next page.
                buf.append(dst)
                buf_src.append(src or dst)
                buf_ids.append(bid)
            flush()
            article.append({
                "key": key,
                "title": tr.get("title") or key,
                "kind": tr.get("kind") or "",
                "page_start": (sec or {}).get("page_start"),
                "page_end": (sec or {}).get("page_end"),
                "paragraphs": paragraphs,
            })

        # Sections the translator skipped entirely (references, appendix, long
        # untranslated bodies) — including modules whose translation file came
        # back empty. The reader names the section a page belongs to instead of
        # pretending there is nothing there.
        translated_titles = {(tr.get("title") or "").strip()
                             for tr in translations.values()
                             if isinstance(tr, dict) and tr.get("translations")}
        untranslated_sections = [
            {"title": (s.get("title") or "").strip() or "未命名章节",
             "page_start": s.get("page_start"), "page_end": s.get("page_end"),
             "block_count": len(s.get("block_ids") or [])}
            for s in section_list
            if (s.get("title") or "").strip() not in translated_titles
            and len(s.get("block_ids") or []) > 0
        ]

        runs = db.runs_for(paper_id)
        paper_graph = app.state.graphs.paper_graph(
            paper_id, include_external=True, max_papers=DEFAULT_SINGLE_MAX_PAPERS)
        return templates.TemplateResponse(request, "paper.html", {
            "paper": row,
            "processing_profile": stored_profile(row),
            "processing_upgrades": {name: estimate(db, stored_profile(row).union(PRESETS[name]), coverage=paper_coverage(base))
                                    for name in ('notes', 'full')},
            "processing_missing": [CAPABILITY_LABELS.get(k, k) for k in sorted(missing_capabilities(stored_profile(row), paper_coverage(base)))],
            "notes": db.notes_for(paper_id),
            "paper_collections": db.collections(paper_id),
            "summary": summary,
            "classification": classification,
            "assessment": assessment,
            "motivation": read_json(base / "motivation.json", None),
            "motivation_neighbors": app.state.graphs.motivation_neighbors(paper_id),
            "relations": relations,
            "glossary": glossary,
            "module_order": [k for k, _ in ordered_modules],
            "chapter_notes": [m for _, m in ordered_modules],
            "article": article,
            "untranslated_sections": untranslated_sections,
            "sections": section_list,
            "runs": runs,
            "error": read_json(base / "error.json", None),
            "taxonomy_paths": _taxonomy_paths(config),
            "all_tags": db.all_tags(),
            "has_pdf": (base / "source.pdf").is_file(),
            "paper_graph": paper_graph,
            "graph_max_papers": DEFAULT_SINGLE_MAX_PAPERS,
            "graph_options": list(SINGLE_MAX_OPTIONS),
        })

    @app.get("/relations", response_class=HTMLResponse)
    def relations_page(request: Request):
        # The page shell never scans paper artifacts or builds the inactive view.
        return templates.TemplateResponse(request, "relations.html", {
            "view": 'tree' if request.query_params.get('view') == 'tree' else 'graph',
            "library_key": str(config.data_dir.resolve()),
            "max_external": DEFAULT_CAPACITY["external"],
            "single_options": list(SINGLE_MAX_OPTIONS),
            "readable": list(READABLE_STATUSES),
        })

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return templates.TemplateResponse(request, "import.html", {
            "recent": db.list_jobs(20),
        })

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return templates.TemplateResponse(request, "jobs.html", {
            "jobs": db.list_jobs(200),
            "papers": {p['id']: project_title(p, paper_dir(config.data_dir, p['id'])) for p in db.list_papers()},
        })

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        run = db.get_run(run_id)
        if not run:
            raise HTTPException(404, detail={"error_code": "not_found", "message": "run not found"})
        # The trace holds the actual prompts and raw response of the call; older
        # runs predate it and say so honestly instead of showing a placeholder.
        trace = None
        if run.get("trace_dir"):
            trace = read_json(Path(run["trace_dir"]) / "trace.json", None)
        return templates.TemplateResponse(request, "run.html",
                                          {"run": run, "trace": trace})

    # --------------------------------------------------------------- api
    @app.get('/api/processing-profiles')
    def api_profiles(paper: str = ''):
        row = db.get_paper(paper) if paper else None
        if paper and row is None:
            return _err(404, 'not_found', '论文不存在')
        covered = paper_coverage(paper_dir(config.data_dir, paper)) if row else set()
        current = stored_profile(row) if row else PRESETS['indexed']
        tasks = task_estimates(db)
        return {'default': default_profile(config.data_dir).to_dict(),
                'current': current.to_dict(), 'coverage': sorted(covered), 'tasks': tasks,
                'presets': [{'id': name, 'label': profile.label, 'profile': profile.to_dict(),
                             'estimate': estimate(db, current.union(profile), coverage=covered, current=current, tasks=tasks)}
                            for name, profile in PRESETS.items()]}

    @app.post('/api/preferences/processing')
    async def api_profile_default(request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        try:
            profile = parse_profile((await _json_body(request)).get('profile'))
            save_default(config.data_dir, profile)
            return {'profile': profile.to_dict()}
        except ValueError as exc:
            return _err(400, 'invalid_profile', str(exc))

    @app.get('/api/graph-readiness')
    def api_graph_readiness(graph: str = 'motivation_tree'):
        try:
            return readiness(db, config.data_dir, graph)
        except ValueError as exc:
            return _err(400, 'invalid_graph', str(exc))

    @app.post('/api/graph-fill')
    async def api_graph_fill(request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        body = await _json_body(request)
        try:
            report = readiness(db, config.data_dir, body.get('graph', 'motivation_tree'))
            selected = body.get('paper_ids')
            if not isinstance(selected, list) or not selected or any(not isinstance(pid, str) for pid in selected):
                raise ValueError('请选择要补齐的论文')
            missing = {p['paper_id']: p for p in report['missing']}
            if not set(selected) <= missing.keys():
                raise ValueError('论文状态已变化，请重新检查')
            items = []
            for pid in dict.fromkeys(selected):
                paper = db.get_paper(pid)
                items.append({'input': paper.get('source_path') or paper.get('source_url') or pid,
                              'paper_id': pid, 'profile': missing[pid]['fill_profile']})
            return app.state.jobs.submit_batch(items)
        except ValueError as exc:
            return _err(400, 'fill_rejected', str(exc))

    @app.post("/api/import/preview")
    async def api_import_preview(request: Request):
        payload = await _json_body(request)
        text = payload.get("input", "")
        if not isinstance(text, str) or not text.strip():
            return _err(400, "bad_request", "请输入论文链接或包含链接的文本。")
        try:
            return await preview_imports(text, db)
        except ValueError as exc:
            return _err(400, "unsupported_input", str(exc))

    @app.post("/api/import")
    async def api_import(request: Request):
        payload = await _json_body(request)
        raw = str(payload.get("input", "")).strip()
        if not raw:
            return _err(400, "bad_request", "input is required")
        title = payload.get("title", "")
        if not isinstance(title, str) or len(title) > 1000:
            return _err(400, "bad_request", "论文标题最多 1000 个字符。")
        # Reject unsupported inputs here rather than in the worker, so the caller
        # gets a specific code instead of a generic queue failure.
        from carrel.ids import UnsupportedInput, resolve_input

        try:
            resolve_input(raw)
        except UnsupportedInput as exc:
            return _err(400, "unsupported_input", str(exc),
                        hint="支持 arXiv 链接、HF Papers 链接、.pdf 直链与本地 PDF 路径")
        force = bool(payload.get("force"))
        try:
            profile = parse_profile(payload.get('profile', default_profile(config.data_dir)))
            job = app.state.jobs.submit(raw, force=force, profile=profile.to_dict(), **({"title": title.strip()} if title.strip() else {}))
        except Exception as exc:  # noqa: BLE001
            return _err(400, "import_rejected", str(exc))
        return {"paper_id": job["paper_id"], "job_id": job["job_id"]}

    @app.post('/api/import/batch')
    async def api_import_batch(request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        payload = await _json_body(request)
        inputs = payload.get('inputs')
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 500:
            return _err(400, 'bad_request', '每批需要 1–500 篇论文。')
        if 'force' in payload and type(payload['force']) is not bool:
            return _err(400, 'bad_request', 'force 须为布尔值。')
        from carrel.ids import resolve_input, UnsupportedInput
        cleaned = []
        try:
            profile = parse_profile(payload.get('profile', default_profile(config.data_dir)))
            for item in inputs:
                if not isinstance(item, dict) or not isinstance(item.get('input'), str) or not isinstance(item.get('title', ''), str):
                    raise ValueError('论文格式无效')
                if len(item.get('title', '')) > 1000:
                    raise ValueError('论文标题最多 1000 个字符')
                raw = item['input'].strip()
                resolve_input(raw)
                cleaned.append(dict(input=raw, title=item.get('title', '').strip(), profile=profile.to_dict()))
            return app.state.jobs.submit_batch(cleaned, force=bool(payload.get('force')))
        except (ValueError, UnsupportedInput) as exc:
            return _err(400, 'unsupported_input', str(exc))
        except (OSError, RuntimeError) as exc:
            return _err(503, 'import_rejected', str(exc))

    @app.get('/api/batches')
    def api_batches():
        return app.state.jobs.scheduler.snapshot()

    @app.get('/api/batches/{batch_id}')
    def api_batch_status(batch_id: str):
        try:
            return app.state.jobs.scheduler.status(batch_id)
        except ValueError as exc:
            return _err(404, 'not_found', str(exc))

    @app.get("/api/papers")
    def api_papers(q: str = "", discipline: str = "", field: str = "", subfield: str = "",
                   tag: str = "", status: str = "", sort: str = "recent", collection: str = ""):
        return {"papers": library_papers(q=q, discipline=discipline, field=field,
                                         subfield=subfield, tag=tag, status=status, sort=sort, collection=collection)}

    def explorer_response(request, payload):
        status = payload.get('status', 'ready')
        if status == 'building':
            return JSONResponse(payload, status_code=202, headers={'Cache-Control': 'no-store'})
        if status == 'error':
            return JSONResponse(payload, status_code=503, headers={'Cache-Control': 'no-store'})
        # Include query parameters and projection shape, not just the library revision.
        from carrel.graph.build import revision_of
        etag = '"' + revision_of(payload) + '"'
        headers = {'ETag': etag, 'Cache-Control': 'private, no-cache'}
        if request.headers.get('if-none-match') == etag and not payload.get('stale'):
            return Response(status_code=304, headers=headers)
        return JSONResponse(payload, headers=headers)

    @app.get('/api/graph/explore')
    def api_graph_explore(request: Request, view: str = 'graph', mode: str = 'overview',
                          group_by: str = 'category', group_id: str = '', focus: str = '',
                          limit: int | None = None, offset: int = 0, q: str = '',
                          keywords_match: str = 'any', sort: str = 'recent',
                          include_context: bool = False, include_similar: bool = False,
                          include_cites: bool = False, edge_limit: int = 40):
        if (view not in ('graph', 'tree') or mode not in ('overview', 'group', 'neighbors', 'branch')
                or group_by not in ('category', 'collection') or keywords_match not in ('any', 'all')
                or sort not in ('recent', 'name', 'depth') or offset < 0 or not 1 <= edge_limit <= 1000
                or (limit is not None and not 1 <= limit <= 200)):
            return _err(422, 'invalid_parameter', '图谱范围或数量无效')
        if mode in ('neighbors',) and not focus:
            return _err(422, 'invalid_parameter', '请选择论文')
        kwargs = dict(view=view, mode=mode, group_by=group_by, group_id=group_id, focus=focus,
                      limit=limit or (5 if mode == 'neighbors' else 12 if mode == 'branch' else 24),
                      offset=offset, q=q.strip()[:200], keywords_match=keywords_match, sort=sort,
                      include_context=include_context, include_similar=include_similar,
                      include_cites=include_cites, edge_limit=edge_limit)
        for name in ('category', 'collection', 'keyword'):
            kwargs[name] = list(dict.fromkeys(v.strip() for v in request.query_params.getlist(name) if v.strip()))
        payload = app.state.graphs.explore(**kwargs)
        if view == 'tree':
            last = db.one("SELECT id, status, error_detail FROM jobs WHERE stage='motivation_tree' ORDER BY rowid DESC LIMIT 1")
            payload.setdefault('stats', {}).update(
                job_id=last['id'] if last else '', job_status=last['status'] if last else '',
                tree_rebuilding=bool(last and last['status'] in ('queued', 'running')),
                tree_error=last['error_detail'] if last and last['status'] == 'failed' else '')
        return explorer_response(request, payload)

    @app.get('/api/graph/citations')
    def api_graph_citations(request: Request, paper_id: str, limit: int = 5):
        visible = list(dict.fromkeys(v.strip() for v in request.query_params.getlist('visible') if v.strip()))
        if not 1 <= limit <= 20 or len(visible) > 500:
            return _err(422, 'invalid_parameter', '引用展开范围或数量无效')
        if not db.get_paper(paper_id):
            return _err(404, 'not_found', '论文不存在')
        payload = app.state.graphs.citation_overlay(paper_id, visible=visible, limit=limit)
        if payload is None:
            return JSONResponse({'status': 'building', 'retry_after_ms': 500}, status_code=202)
        return explorer_response(request, payload)

    @app.get('/api/graph/locate')
    def api_graph_locate(request: Request, q: str = '', view: str = 'graph', limit: int = 8):
        if view not in ('graph', 'tree') or not 1 <= limit <= 12:
            return _err(422, 'invalid_parameter', '定位范围或数量无效')
        collections = list(dict.fromkeys(v.strip() for v in request.query_params.getlist('collection') if v.strip()))
        payload = app.state.graphs.locate(q.strip()[:200], view=view, collection=collections, limit=limit)
        return explorer_response(request, payload)

    @app.get('/api/graph/node')
    def api_graph_node(request: Request, paper_id: str):
        if not db.get_paper(paper_id):
            return _err(404, 'not_found', '论文不存在')
        payload = app.state.graphs.node_detail(paper_id)
        if payload is None:
            return JSONResponse({'status': 'building', 'retry_after_ms': 500}, status_code=202)
        return explorer_response(request, payload)

    @app.post('/api/graph/refresh')
    def api_graph_refresh(request: Request):
        if error := _settings_csrf(request):
            return error
        app.state.graphs.refresh()
        return JSONResponse({'status': 'building', 'retry_after_ms': 500}, status_code=202)

    @app.post('/api/graph/node-label')
    async def api_graph_node_label(request: Request):
        if error := _settings_csrf(request):
            return error
        payload = await _json_body(request)
        paper_id = payload.get('paper_id')
        if not isinstance(paper_id, str) or not db.get_paper(paper_id):
            return _err(404, 'not_found', '论文不存在')
        from carrel.graph.display import save_display_name
        from carrel.runtime.locks import paper_locks, path_key
        try:
            with paper_locks.hold((path_key(config.data_dir), paper_id)):
                label = save_display_name(config.data_dir, paper_id, payload.get('label', ''))
            app.state.graphs.invalidate()
        except ValueError as exc:
            return _err(422, 'invalid_parameter', str(exc))
        return {'ok': True, 'paper_id': paper_id, 'label': label}

    @app.post('/api/graph/branch-label')
    async def api_graph_branch_label(request: Request):
        if error := _settings_csrf(request):
            return error
        payload = await _json_body(request)
        branch_id, label = payload.get('branch_id'), payload.get('label', '')
        if (not isinstance(branch_id, str) or not branch_id.startswith('branch:')
                or len(branch_id) > 100 or not isinstance(label, str)
                or len(label.strip()) > 60 or any(ord(c) < 32 for c in label)):
            return _err(422, 'invalid_parameter', '主线名称最多 60 个字符，请使用单行简短名称')
        overview = app.state.graphs.explore(view='tree', mode='overview')
        if overview.get('status') != 'ready':
            return _err(409, 'not_ready', '主线正在准备，请稍后重试')
        if not any(group['id'] == branch_id for group in overview.get('groups', [])):
            return _err(404, 'not_found', '主线已变更，请刷新后重试')
        from carrel.graph.display import save_branch_name
        label = save_branch_name(config.data_dir, branch_id, label)
        app.state.graphs.invalidate()
        return {'ok': True, 'branch_id': branch_id, 'label': label}

    @app.get("/api/graph")
    def api_graph(include_external: bool = False, max_external: int = DEFAULT_CAPACITY["external"],
                  discipline: str = "", field: str = "", subfield: str = ""):
        """Whole-library topology for the graph view.

        Read-only: it projects SQLite and the classification files and never
        writes. Booleans and integers are typed by FastAPI, so a malformed value
        is a 422 with the offending parameter rather than a silent default.
        """
        if not 0 <= max_external <= DEFAULT_CAPACITY["external"]:
            return _err(422, "invalid_parameter",
                        f"max_external 需要在 0 到 {DEFAULT_CAPACITY['external']} 之间")
        graph = app.state.graphs.library_graph(
            include_external=include_external, max_external=max_external,
            discipline=discipline, field=field, subfield=subfield)
        return graph.to_dict()

    def motivation_tree_graph(focus=""):
        # Read the job first: its terminal state is written after the artifact.
        # The reverse order can return an old graph with a completed job and
        # make the browser stop polling before it sees the new graph.
        last = db.one("SELECT * FROM jobs WHERE stage='motivation_tree' ORDER BY rowid DESC LIMIT 1")
        graph = app.state.graphs.motivation_tree(focus)
        graph.stats.update(rebuilding=False, job_id="", job_status="", error="")
        if last:
            graph.stats.update(rebuilding=last["status"] in ("queued", "running"),
                               job_id=last["id"], job_status=last["status"],
                               error=last["error_detail"] if last["status"] == "failed" else "")
        return graph

    @app.get("/api/motivation-tree")
    def api_motivation_tree(focus: str = ""):
        return motivation_tree_graph(focus).to_dict()

    @app.post("/api/motivation-tree/rebuild")
    def api_motivation_tree_rebuild():
        job = app.state.jobs.submit("", kind="motivation_tree", force=True)
        if job.get("duplicate"):
            return JSONResponse(status_code=409, content={**job, "error_code": "already_queued",
                                 "message": "已在梳理中", "hint": "完成后此页面会自动更新。"})
        return job

    # Sub-resource routes must be declared before the catch-all ``{paper_id:path}``
    # route, otherwise Starlette matches the catch-all first.
    @app.get("/api/papers/{paper_id:path}/module/{kind}")
    def api_module(paper_id: str, kind: str):
        data = read_json(paper_dir(config.data_dir, paper_id) / "modules" / f"{kind}.json", None)
        if data is None:
            return _err(404, "not_found", f"module {kind} not found")
        return data

    @app.get("/api/papers/{paper_id:path}/graph")
    def api_paper_graph(paper_id: str, include_external: bool = True,
                        max_papers: int = DEFAULT_SINGLE_MAX_PAPERS,
                        max_external: int | None = None):
        """One paper's neighbourhood. Declared above the catch-all route."""
        if max_papers not in SINGLE_MAX_OPTIONS:
            return _err(422, "invalid_parameter",
                        f"max_papers 可选值：{'、'.join(map(str, SINGLE_MAX_OPTIONS))}")
        if max_external is not None and not 0 <= max_external <= DEFAULT_CAPACITY["external"]:
            return _err(422, "invalid_parameter", "max_external 需要在 0 到 40 之间")
        graph = app.state.graphs.paper_graph(
            paper_id, include_external=include_external, max_papers=max_papers, max_external=max_external)
        if graph is None:
            return _err(404, "not_found", f"paper {paper_id} not found")
        return graph.to_dict()

    @app.get("/api/papers/{paper_id:path}/relations")
    def api_relations(paper_id: str, kind: str = ""):
        return {"relations": db.relations_for(paper_id, kind)}

    @app.get("/api/papers/{paper_id:path}/assessment")
    def api_assessment(paper_id: str):
        return read_json(paper_dir(config.data_dir, paper_id) / "assessment.json", {}) or {}

    @app.get("/api/papers/{paper_id:path}/pdf")
    def api_pdf(paper_id: str):
        path = paper_dir(config.data_dir, paper_id) / "source.pdf"
        if not path.is_file():
            return _err(404, "not_found", "pdf not available")
        return FileResponse(path, media_type="application/pdf",
                            headers={"Content-Disposition": "inline"})

    @app.post("/api/papers/{paper_id:path}/category")
    async def api_set_category(paper_id: str, request: Request):
        if error := _settings_csrf(request):
            return error
        payload = await _json_body(request)
        from carrel.runtime.locks import paper_locks, path_key
        with paper_locks.hold((path_key(config.data_dir), paper_id)), taxonomy_lock:
            return set_category(paper_id, payload)

    def set_category(paper_id, payload):
        discipline = str(payload.get("discipline", "")).strip()
        field_name = str(payload.get("field", "")).strip()
        subfield = str(payload.get("subfield", "")).strip()
        if not db.get_paper(paper_id):
            return _err(404, "not_found", "paper not found")
        # Keep classification.json in step so the page shows the edited path,
        # not the stale model output, on the next render.
        base = paper_dir(config.data_dir, paper_id)
        clf = read_json(base / "classification.json", {}) or {}
        categories = paper_categories(db.get_paper(paper_id)) if clf.get('categories') else []
        vocabulary = Taxonomy.layered(Path(__file__).resolve().parents[1] / 'taxonomy.toml', config.data_dir / 'taxonomy.toml')
        category = project_path({"discipline": discipline, "field": field_name, "subfield": subfield}, vocabulary.renames)
        category['category_id'] = vocabulary.id_for(category['discipline'], category['field'])
        if field_name and not discipline:
            return _err(400, 'bad_request', '请先填写一级研究方向')
        if any(len(value) > 80 or any(ord(c) < 32 for c in value) for value in (discipline, field_name, subfield)):
            return _err(400, 'bad_request', '分类名称最多 80 个字符')
        index = payload.get("index")
        if index is not None:
            if type(index) is not int or index < 0 or index > len(categories):
                return _err(400, "bad_request", "分类位置无效，请刷新后重试")
            if payload.get("remove") is True:
                if index == len(categories):
                    return _err(400, "bad_request", "分类不存在，请刷新后重试")
                categories.pop(index)
            elif not discipline:
                return _err(400, "bad_request", "请填写学科")
            elif index == len(categories):
                categories.append(category)
            else:
                if categories[index].get('category_id') == clf.get('primary_category_id'):
                    clf['primary_category_id'] = category['category_id']
                categories[index] = category
        else:
            categories = [category] if discipline else []
        clf["categories"] = categories
        clf['origin'] = 'human'
        primary = next((c for c in categories if c.get('category_id') == clf.get('primary_category_id')),
                       categories[0] if categories else {})
        clf['primary_category_id'] = primary.get('category_id', '')
        clf.pop('subfield', None)
        for key in ("discipline", "field"):
            clf[key] = primary.get(key, "")
        path = base / 'classification.json'
        original = path.read_text(encoding='utf-8') if path.exists() else None
        try:
            with db.transaction() as conn:
                atomic_write_text(path, json.dumps(clf, ensure_ascii=False, indent=2))
                conn.execute("UPDATE papers SET discipline=?, field=?, updated_at=? WHERE id=?",
                             (clf["discipline"], clf["field"], _now(), paper_id))
        except Exception:
            if original is None:
                path.unlink(missing_ok=True)
            elif path.read_text(encoding='utf-8') != original:
                atomic_write_text(path, original)
            return _err(500, 'save_failed', '分类保存失败，请重试')
        return {"ok": True, "categories": categories, **category}

    @app.post("/api/papers/{paper_id:path}/tags")
    async def api_set_tags(paper_id: str, request: Request):
        payload = await _json_body(request)
        if not db.get_paper(paper_id):
            return _err(404, "not_found", "paper not found")
        add = payload.get("add") or []
        remove = payload.get("remove") or []
        if not isinstance(add, list) or not isinstance(remove, list):
            return _err(400, "bad_request", "标签必须为列表")
        normalized = []
        for item in add:
            if isinstance(item, str):
                item = {"tag": item, "evidence": "", "block_id": ""}
            if not isinstance(item, dict) or not isinstance(item.get("tag"), str):
                return _err(400, "bad_request", "标签格式无效")
            label = item["tag"].strip()
            if not label or len(label) > 24:
                return _err(400, "bad_request", "标签需要 1 至 24 个字符")
            normalized.append((label, str(item.get("evidence", "")),
                               str(item.get("block_id") or item.get("source_block") or "")))
        if any(not isinstance(tag, str) for tag in remove):
            return _err(400, "bad_request", "标签格式无效")
        # Renames are one transaction, so a failed addition cannot lose the old tag.
        with db.transaction() as conn:
            for tag in remove:
                conn.execute("DELETE FROM tags WHERE paper_id=? AND tag=?", (paper_id, tag))
            for label, evidence, block in normalized:
                conn.execute(
                    "INSERT INTO tags(paper_id, tag, evidence, source_block, origin)"
                    " VALUES(?,?,?,?,'human') ON CONFLICT(paper_id, tag) DO NOTHING",
                    (paper_id, label, evidence, block),
                )
        return {"ok": True, "tags": db.tags_for(paper_id)}

    @app.get("/api/taxonomy")
    def api_taxonomy():
        return {"tree": used_categories(include_custom=True), "proposals": db.list_proposals()}

    @app.post("/api/taxonomy")
    async def api_taxonomy_add(request: Request):
        """Persist a two-level research direction/topic to the user vocabulary."""
        if error := _settings_csrf(request):
            return error
        payload = await _json_body(request)
        discipline = str(payload.get("discipline", "")).strip()
        field_name = str(payload.get("field", "")).strip()
        subfield = str(payload.get("subfield", "")).strip()
        if subfield:
            return _err(422, 'bad_request', '分类现为两级，请将具体方法作为关键词')
        if not discipline or (not field_name and discipline in
                              _packaged_disciplines(config)):
            return _err(400, "bad_request",
                        "新增需要 discipline + field（或新 discipline 本身）")
        from pathlib import Path as _P

        from carrel.classify.taxonomy import Taxonomy

        with taxonomy_lock:
            overlay_path = _P(config.data_dir) / "taxonomy.toml"
            user = Taxonomy.load(overlay_path, fallback=False)
            # The overlay only holds the user's additions; start from an empty tree.
            from carrel.classify.taxonomy import Taxonomy as _T

            additions = _T(disciplines=[], fields={})
            additions.disciplines = [discipline] if discipline not in user.disciplines else []
            additions.fields = {discipline: [field_name] if field_name else []}
            merged_user = user.merged(additions)
            atomic_write_text(overlay_path, merged_user.to_toml())
            db.sync_categories(Taxonomy.layered(
                Path(__file__).resolve().parents[1] / "taxonomy.toml", overlay_path).category_rows())
            return {"ok": True, "discipline": discipline,
                    "field": field_name}

    @app.post("/api/taxonomy/rename")
    async def api_taxonomy_rename(request: Request):
        if error := _settings_csrf(request):
            return error
        payload = await _json_body(request)
        source = payload.get("source")
        name = payload.get("name")
        if (not isinstance(source, dict) or not isinstance(name, str)
                or any(not isinstance(source.get(key, ""), str) for key in ('discipline', 'field', 'subfield'))):
            return _err(400, "bad_request", "分类格式无效")
        if source.get('subfield'):
            source = project_path(source)
        source = {key: source.get(key, "").strip() for key in ('discipline', 'field')}
        name = name.strip()
        if (not source["discipline"]
                or not name or any(len(value) > 80 or any(ord(c) < 32 for c in value)
                                   for value in [name, *source.values()])):
            return _err(400, "bad_request", "分类名称需要 1 至 80 个字符，且层级完整")
        if name == "未分类":
            return _err(400, "bad_request", "请使用具体的分类名称")
        key = 'field' if source['field'] else 'discipline'
        with taxonomy_lock:
            overlay_path = Path(config.data_dir) / "taxonomy.toml"
            packaged = Path(__file__).resolve().parents[1] / "taxonomy.toml"
            taxonomy = Taxonomy.layered(packaged, overlay_path)
            papers = db.list_papers()
            known = taxonomy.merged(Taxonomy())
            for paper in papers:
                for category in paper_categories(paper):
                    known.add_path(category)
            if not any(all(path.get(k, '') == v for k, v in source.items()) for path in known.paths()):
                return _err(404, "not_found", "分类已变更，请刷新后重试")
            if name == source[key]:
                return {"ok": True, "updated_papers": 0}
            if any(all(path.get(k, '') == v for k, v in {**source, key: name}.items()) for path in known.paths()):
                return _err(409, "conflict", "同一层级已有此分类，请使用其他名称")

            user = Taxonomy.load(overlay_path, fallback=False).renamed(source, name)
            user.renames.append({**source, "name": name})
            for path in known.paths():
                if matches_category(path, source) and path not in taxonomy.paths():
                    user.add_path({**path, key: name})

            updates, files = [], []
            for paper in papers:
                path = paper_dir(config.data_dir, paper["id"]) / "classification.json"
                classification = read_json(path, {}) or {}
                categories = paper_categories(paper)
                changed = False
                for category in categories:
                    if matches_category(category, source):
                        category[key] = name
                        changed = True
                if not changed:
                    continue
                classification["categories"] = categories
                primary = next((c for c in categories if c.get('category_id') == classification.get('primary_category_id')), categories[0])
                classification['primary_category_id'] = primary.get('category_id', '')
                classification.pop('subfield', None)
                for part in ('discipline', 'field'):
                    classification[part] = primary.get(part) or ""
                updates.append((classification["discipline"], classification["field"],
                                _now(), paper["id"]))
                files.append((path, json.dumps(classification, ensure_ascii=False, indent=2)))
            files.append((overlay_path, user.to_toml()))

            # SQLite can roll back its changes; retain originals for file rollback too.
            originals = {path: path.read_text(encoding="utf-8") if path.is_file() else None
                         for path, _ in files}
            written = []
            try:
                with db.transaction() as conn:
                    for path, content in files:
                        atomic_write_text(path, content)
                        written.append(path)
                    conn.executemany("UPDATE papers SET discipline=?, field=?, updated_at=? WHERE id=?", updates)
                    conn.execute("DELETE FROM categories")
                    rows = Taxonomy.layered(packaged, overlay_path).category_rows()
                    conn.executemany("INSERT INTO categories(path,label,parent,sort) VALUES(?,?,?,?)",
                                     [(row["path"], row["label"], row["parent"], row["sort"]) for row in rows])
            except Exception:
                for path in reversed(written):
                    if originals[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write_text(path, originals[path])
                return _err(500, "save_failed", "分类保存失败，请重试")
            return {"ok": True, "updated_papers": len(updates)}

    def _packaged_disciplines(config) -> set:
        from pathlib import Path as _P

        from carrel.classify.taxonomy import Taxonomy

        return set(Taxonomy.load(
            _P(__file__).resolve().parents[1] / "taxonomy.toml").disciplines)

    def _taxonomy_paths(config) -> list[dict[str, str]]:
        """Full (packaged + user overlay) path list for the facet input."""
        from pathlib import Path as _P

        from carrel.classify.taxonomy import Taxonomy

        return Taxonomy.layered(
            _P(__file__).resolve().parents[1] / "taxonomy.toml",
            _P(config.data_dir) / "taxonomy.toml",
        ).paths()

    def _custom_paths(config):
        overlay = Path(config.data_dir) / "taxonomy.toml"
        try:
            return Taxonomy.load(overlay, fallback=False).paths()
        except (ValueError, OSError):
            return []

    @app.post("/api/proposals/{proposal_id}/resolve")
    async def api_resolve(proposal_id: str, request: Request):
        payload = await _json_body(request)
        status = str(payload.get("status", "accepted"))
        db.resolve_proposal(proposal_id, status)
        return {"ok": True, "status": status}

    @app.post("/api/papers/{paper_id:path}/title")
    async def api_title(paper_id: str, request: Request):
        if rejected := _settings_csrf(request):
            return rejected
        if not db.get_paper(paper_id):
            return _err(404, 'not_found', '论文不存在')
        payload = await _json_body(request)
        import asyncio
        from carrel.runtime.locks import paper_locks, path_key

        def update():
            with paper_locks.hold((path_key(config.data_dir), paper_id), blocking=False) as acquired:
                if not acquired:
                    return _err(409, 'paper_busy', '论文正在处理，请完成后再修改标题')
                try:
                    if payload.get('action') == 'retry':
                        result = ensure_title(config, db, router, paper_id, retry=True)
                    elif payload.get('action') == 'save':
                        result = save_human_title(config, db, paper_id, payload.get('title_zh'))
                    else:
                        return _err(400, 'invalid_action', '请选择重新生成或保存标题')
                except ValueError as exc:
                    return _err(400, 'invalid_title', str(exc))
                app.state.graphs.invalidate()
                return {'status': result['status'], 'issues': result.get('issues', []),
                        'title_zh': result.get('title_zh', '')}

        return await asyncio.to_thread(update)

    @app.post("/api/papers/{paper_id:path}/retry")
    async def api_retry(paper_id: str, request: Request, force: int = 0):
        """Re-run a paper. Default: finish what is missing (a `partial` paper
        re-translates only its failed sections). ``?force=1`` regenerates every
        stage, including a fresh parse."""
        row = db.get_paper(paper_id)
        if not row:
            return _err(404, "not_found", "paper not found")
        if rejected := _settings_csrf(request):
            return rejected
        payload = await _json_body(request)
        try:
            profile = parse_profile(payload.get('profile', default_profile(config.data_dir) if row.get('status') == 'indexed' else stored_profile(row)))
        except ValueError as exc:
            return _err(400, 'invalid_profile', str(exc))
        if row.get("status") == "indexed" and row.get('source_kind') == 'local_path':
            source = Path(row.get("source_path") or "")
            if not source.is_file():
                return _err(409, "source_missing", "本地 PDF 已移动或无法访问，请重新扫描原目录")
            from carrel.ids import sha256_file

            if sha256_file(source) != row.get("pdf_sha256"):
                return _err(409, "source_changed", "本地 PDF 内容已变化，请重新扫描后再处理")
        # The pipeline resolves *inputs*, not ids: a local PDF import is stored
        # as ``pdf-<sha>``, which is not something resolve_input understands.
        # Hand it the original source the record was created from.
        raw = row.get("source_path") or row.get("source_url") or paper_id
        if raw.startswith("pdf-") and not (row.get("source_path") or row.get("source_url")):
            return _err(409, "no_source", "paper has no stored source to re-run from")
        indexed = row.get("status") == "indexed"
        if indexed:
            db.execute("UPDATE papers SET status='queued', profile=? WHERE id=? AND status='indexed'",
                       (json.dumps(stored_profile(row).to_dict()), paper_id))
        try:
            job = app.state.jobs.submit(raw, force=bool(force), paper_id=paper_id, profile=profile.to_dict())
        except Exception:
            if indexed:
                db.execute("UPDATE papers SET status='indexed' WHERE id=? AND status='queued'", (paper_id,))
            raise
        return job

    @app.get("/api/jobs")
    def api_jobs():
        return {"jobs": db.list_jobs(200)}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        run = db.get_run(run_id)
        return run or _err(404, "not_found", "run not found")

    @app.get("/api/health")
    def api_health():
        return _health(config)

    @app.get("/api/usage")
    def api_usage(paper: str = "", since: str = ""):
        """Model spend by task; the same report as ``carrel usage``."""
        return db.usage_summary(paper_id=paper or None, since=since or None)

    @app.get("/api/papers/{paper_id:path}/annotations")
    def api_annotations_get(paper_id: str):
        if not db.get_paper(paper_id):
            return _err(404, "not_found", "paper not found")
        return read_json(paper_dir(config.data_dir, paper_id) / "annotations.json", {}) \
            or {"pages": {}}

    @app.post("/api/papers/{paper_id:path}/annotations")
    async def api_annotations_post(paper_id: str, request: Request):
        if not db.get_paper(paper_id):
            return _err(404, "not_found", "paper not found")
        payload = await _json_body(request)
        # Whole-file replace: the client sends its complete stroke set, which it
        # keeps in memory, so last-writer-wins is the intended semantics.
        if not isinstance(payload, dict) or "pages" not in payload:
            return _err(400, "bad_request", "payload must contain 'pages'")
        from carrel.store.paths import atomic_write_json

        atomic_write_json(paper_dir(config.data_dir, paper_id) / "annotations.json",
                          {"pages": payload.get("pages") or {}})
        return {"ok": True, "pages": len(payload.get("pages") or {})}

    @app.get("/api/papers/{paper_id:path}/progress")
    def api_progress(paper_id: str):
        """Live pipeline state for the paper page's progress bar.

        Counts come from artifacts on disk, so this reflects reality even while
        the pipeline is mid-stage (e.g. 7 of 13 modules translated).
        """
        row = db.get_paper(paper_id)
        if not row:
            return _err(404, "not_found", f"paper {paper_id} not found")
        base = paper_dir(config.data_dir, paper_id)
        meta = read_json(base / "progress_meta.json", {}) or {}

        modules_done = len(list((base / "modules").glob("*.json"))) \
            if (base / "modules").is_dir() else 0
        translations_done = len(
            [p for p in (base / "translation").glob("*.json")
             if not p.name.startswith("_")]
        ) if (base / "translation").is_dir() else 0

        status = row.get("status") or "queued"
        order = ["parsing", "structuring", "translating", "classifying", "relating"]
        current = status if status in order else None
        stages = {}
        if current is None:
            all_done = status not in ("queued", "indexed")
            for name in order:
                stages[name] = "done" if all_done else "pending"
            if status == "partial":
                stages["translating"] = "failed"
        else:
            reached = False
            for name in order:
                if name == current:
                    stages[name] = "running"
                    reached = True
                else:
                    stages[name] = "pending" if reached else "done"

        requested = stored_profile(row)
        missing = missing_capabilities(requested, paper_coverage(base))
        if not requested.translate:
            stages['translating'] = 'skipped'
        elif status == 'partial' and 'translated' not in missing:
            stages['translating'] = 'done'
        if not any((requested.classify, requested.assess, requested.motivation)):
            stages['classifying'] = 'skipped'
        elif status == 'partial' and missing & {'classified', 'assessed', 'motivation'}:
            stages['classifying'] = 'failed'
        if not requested.relations:
            stages['relating'] = 'skipped'

        return {
            "paper_id": paper_id,
            "status": status,
            "terminal": status in ("ready", "ready_with_warning", "partial", "failed", "indexed"),
            "profile": stored_profile(row).to_dict(),
            "coverage": sorted(paper_coverage(base)),
            "missing": sorted(missing_capabilities(stored_profile(row), paper_coverage(base))),
            "stages": stages,
            "modules": {"done": modules_done,
                        "total": meta.get("modules_total", modules_done)},
            "translations": {"done": translations_done,
                             "total": meta.get("translations_total", translations_done)},
            "artifacts": {
                "doc": (base / "doc.json").is_file(),
                "preview": (base / "preview.json").is_file(),
                "summary": (base / "summary.json").is_file(),
                "classification": (base / "classification.json").is_file(),
                "assessment": (base / "assessment.json").is_file(),
                "relations": (base / "relations.json").is_file(),
            },
            "error": read_json(base / "error.json", None),
        }

    @app.get("/api/papers/{paper_id:path}/text")
    def api_text(paper_id: str, mode: str = "orig", module: str = ""):
        base = paper_dir(config.data_dir, paper_id)
        doc = read_json(base / "doc.json", {}) or {}
        blocks = doc.get("blocks", [])
        if mode == "orig" or not module:
            return {"blocks": blocks}
        tr = read_json(base / "translation" / f"{module}.json", {}) or {}
        return {"blocks": blocks, "translations": tr.get("translations", {})}

    # Catch-all paper route last: Starlette matches in declaration order, so any
    # ``/api/papers/{id}/<sub>`` route above this must be declared first.
    @app.get("/api/papers/{paper_id:path}")
    def api_paper(paper_id: str):
        row = db.get_paper(paper_id)
        if not row:
            return _err(404, "not_found", f"paper {paper_id} not found")
        base = paper_dir(config.data_dir, paper_id)
        row["summary"] = read_json(base / "summary.json", {}) or {}
        row["classification"] = read_json(base / "classification.json", {}) or {}
        row["tags"] = db.tags_for(paper_id)
        row["sections"] = read_json(base / "sections.json", {}) or {}
        row["runs"] = db.runs_for(paper_id)
        mod_dir = base / "modules"
        row["modules"] = sorted(p.stem for p in mod_dir.glob("*.json")) if mod_dir.is_dir() else []
        return project_title(row, base)

    return app


# --------------------------------------------------------------- helpers

class _JobRunner:
    """Web compatibility adapter; all execution belongs to ImportScheduler."""

    def __init__(self, config: AppConfig, db: Database, *, backend=None) -> None:
        from carrel.scheduler import ImportScheduler
        self.scheduler = ImportScheduler(config, db, backend=backend)
        self.backend = self.scheduler.pipeline.backend
        db.execute("UPDATE jobs SET status='failed', finished_at=?, error_code='interrupted',"
                   " error_detail='上次梳理因服务停止而中断，请重新梳理。'"
                   " WHERE stage='motivation_tree' AND status IN ('running','queued')", (_now(),))

    def submit(self, raw: str, *, force=False, paper_id='', title='', kind='pipeline', key='', trailing=False, profile=None):
        if kind == 'motivation_tree':
            return self.scheduler.submit_library(force=force, key=key)
        if kind != 'pipeline':
            raise ValueError('Unknown job kind')
        item = dict(input=raw, paper_id=paper_id, title=title)
        if profile is not None:
            item['profile'] = profile
        result = self.submit_batch([item], force=force)
        return {**result['jobs'][0], 'batch_id': result['batch_id']}

    def submit_batch(self, inputs, *, force=False):
        batch_id = self.scheduler.submit(inputs, force=force)
        batch = self.scheduler.status(batch_id)
        return dict(batch_id=batch_id, jobs=[dict(job_id=e['job_id'], paper_id=e['paper_id']) for e in batch['items']])

    def progress(self, paper_id):
        return self.scheduler.progress(paper_id)


def _preview_id(raw: str) -> str:
    from carrel import ids as ids_mod

    try:
        return ids_mod.resolve_input(raw).paper_id
    except Exception:  # noqa: BLE001
        return "unknown"


def _static_files():
    from fastapi.staticfiles import StaticFiles

    return StaticFiles(directory=str(STATIC_DIR))


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _err(status: int, code: str, message: str, hint: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error_code": code, "message": message, "hint": hint},
    )


def _health(config: AppConfig) -> dict[str, Any]:
    return {
        "providers": [{"id": p.id, "label": p.label, "protocol": p.protocol,
                       "base_url": p.base_url, "model": p.model, "configured": p.configured}
                      for p in config.providers.values()],
        "default_provider": config.default_provider or "legacy",
        "task_routes": {t: r.to_dict() for t, r in config.task_routes.items()},
        "mineru": {"configured": config.mineru.enabled,
                   "model_version": config.mineru.model_version},
        "model": {"protocol": config.model.protocol, "name": config.model.name,
                  "base_url": config.model.base_url or "(default)",
                  "configured": bool(config.model.api_key)},
        "sciverse": {"configured": config.sciverse.enabled},
        "openalex": {"mailto": bool(config.openalex.mailto)},
        "data_dir": str(config.data_dir),
    }


def _settings_csrf(request: Request):
    site = request.headers.get("sec-fetch-site")
    if site not in ("same-origin", "none") and (site is not None or request.headers.get("x-carrel") != "1"):
        return _err(403, "cross_site_request", "此操作须来自本地 Carrel 页面", "脚本调用请发送 X-Carrel: 1 请求头")
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def app_factory():
    """Zero-argument factory for ``uvicorn --factory`` and ASGI servers.

    Reads configuration from the environment / ``config/local.toml`` and opens the
    database in the configured data directory.
    """
    from carrel.config import load_config
    from carrel.store.db import Database

    config = load_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.data_dir / "app.db")
    return create_app(config, db)
