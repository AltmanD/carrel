"""Explicit, paid provider checks; never run during application startup."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from carrel.config import DEFAULT_MAX_TOKENS, MODEL_TASKS
from carrel.model.base import extract_json, validate_schema
from carrel.model.router import ProviderRouter
from carrel.providers import RouteConfig
from carrel.store.paths import atomic_write_json, paper_dir, read_json, safe_dirname
from carrel.translate.qa import HARD_CHECKS, check_block

VERIFY_VERSION = 3


def recommended_routes(provider):
    return {task: RouteConfig(provider.id, provider.model, provider.thinking or "low",
                              DEFAULT_MAX_TOKENS[task]) for task in MODEL_TASKS}


def fingerprint(provider):
    values = provider.to_dict()
    values["api_key"] = provider.effective_api_key
    for key in ("label", "notes", "api_key_env", "max_concurrency", "min_concurrency"):
        values.pop(key, None)
    values["routes"] = {t: r.to_dict() for t, r in recommended_routes(provider).items()}
    values["version"] = VERIFY_VERSION
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def report_path(data_dir, provider_id):
    return Path(data_dir) / "provider-verifications" / f"{provider_id}.json"


def verification_status(data_dir, provider):
    report = read_json(report_path(data_dir, provider.id), {}) or {}
    if not isinstance(report, dict):
        report = {}
    current = report.get("fingerprint") == fingerprint(provider)
    rows = report.get("tasks", [])
    passed = (current and report.get("passed") is True and report.get("baseline_passed") is True
              and isinstance(rows, list) and len(rows) == len(MODEL_TASKS) and all(isinstance(r, dict) for r in rows)
              and {r.get("task") for r in rows} == set(MODEL_TASKS)
              and all(r.get("passed") is True for r in rows))
    return {"passed": bool(passed), "current": current, "report": report}


def probe_provider(provider, fallback_config, *, backend_factory=None):
    """A small connectivity probe is deliberately not a task-validation pass."""
    started = time.monotonic()
    try:
        if not provider.configured or not provider.model:
            raise ValueError("请先填写 API 地址、密钥和模型名")
        router = ProviderRouter({provider.id: replace(provider, thinking="disabled", max_retries=0)}, {},
                                default_provider=provider.id, fallback_config=fallback_config,
                                **({"backend_factory": backend_factory} if backend_factory else {}))
        reply = router.raw_structured(schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                                             "required": ["ok"], "additionalProperties": False},
                                      system="Reply with JSON only.", user='Return {"ok": true}.',
                                      max_tokens=512)
        if reply.data != {"ok": True} or reply.truncated:
            raise ValueError("端点未返回完整的 {ok: true} 对象")
        return {"ok": True, "model": reply.model, "requested_model": reply.requested_model,
                "substituted": reply.substituted, "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": provider.redact(str(exc))[:2000],
                "latency_ms": round((time.monotonic() - started) * 1000)}


def load_sample(data_dir, paper_id=""):
    """Read real introduction blocks and existing extracts without modifying papers."""
    # Titles and canonical IDs live in SQLite, not in a per-paper meta.json.
    # Open read-only so verification cannot migrate a running library's DB.
    metadata = {}
    db_path = Path(data_dir) / "app.db"
    if db_path.is_file():
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            metadata = {safe_dirname(row["id"]): dict(row)
                        for row in db.execute("SELECT id, title, year FROM papers")}
    folders = [paper_dir(Path(data_dir), paper_id)] if paper_id else sorted((Path(data_dir) / "papers").glob("*"))
    for folder in folders:
        doc = read_json(folder / "doc.json", {}) or {}
        sections = read_json(folder / "sections.json", {}) or {}
        sections = sections.get("sections", []) if isinstance(sections, dict) else sections
        ids = {bid for s in sections if s.get("kind") == "introduction" for bid in s.get("block_ids", [])}
        blocks = [b for b in doc.get("blocks", []) if b.get("id") in ids and b.get("type") == "text" and b.get("text", "").strip()]
        # Whole blocks only: cutting a source paragraph would invalidate local QA.
        selected, size = [], 0
        for b in blocks:
            if len(selected) >= 4 or size + len(b["text"]) > 6000:
                break
            selected.append(b)
            size += len(b["text"])
        if not selected:
            continue
        meta = metadata.get(folder.name) or read_json(folder / "meta.json", {}) or {}
        extracts = [read_json(p, {}) for p in sorted((folder / "modules").glob("*.json"))]
        selected_ids = {b["id"] for b in selected}
        extracts = [{"kind": e.get("kind"), "digest": str(e.get("digest", ""))[:800],
                     "points": [p for p in e.get("points", []) if p.get("block_id") in selected_ids][:8],
                     "definitions": e.get("definitions", [])[:6]}
                    for e in extracts if isinstance(e, dict) and e.get("kind") == "introduction"]
        summary = read_json(folder / "summary.json", {}) or {}
        summary = {k: summary[k] for k in ("one_line", "tldr", "contributions") if k in summary}
        summary = {k: str(v)[:1500] if isinstance(v, str) else v[:4] for k, v in summary.items()}
        glossary = read_json(folder / "glossary.json", {}) or {}
        source = " ".join(b["text"] for b in selected).lower()
        glossary = {"terms": [t for t in glossary.get("terms", []) if str(t.get("term", t.get("en", ""))).lower() in source][:12]}
        translations = {}
        for path in sorted((folder / "translation").glob("*.json")):
            translations.update((read_json(path, {}) or {}).get("translations", {}))
        neighbors = []
        for other in sorted((Path(data_dir) / "papers").glob("*")):
            if other == folder:
                continue
            motivation = read_json(other / "motivation.json", {}) or {}
            if motivation.get("motivation"):
                info = metadata.get(other.name) or read_json(other / "meta.json", {}) or {}
                neighbors.append({"id": info.get("id") or other.name, "title": info.get("title") or other.name,
                                  "motivation": motivation.get("motivation"), "year": info.get("year")})
            if len(neighbors) == 5:
                break
        return {"paper": meta.get("id") or paper_id or folder.name, "title": meta.get("title") or folder.name, "year": meta.get("year"), "blocks": selected,
                "summary": summary, "extracts": extracts,
                "glossary": glossary, "translations": translations,
                "motivation": read_json(folder / "motivation.json", {}) or {}, "neighbors": neighbors}
    raise ValueError("未找到有引言原文的论文；请先导入一篇论文，或用 --paper 指定已有论文 ID")


def task_cases(sample):
    from carrel.titles import TITLE_SCHEMA, TITLE_SYSTEM
    from carrel.structure import preview, extract, summarize
    from carrel.translate import glossary, translator, term_review
    from carrel.classify import classifier
    from carrel.classify.taxonomy import Taxonomy
    from carrel.assess import evaluate
    from carrel.motivation import extract as motivation, tree

    rendered = translator._render_blocks(sample["blocks"])
    material = json.dumps({k: v for k, v in sample.items() if k not in {"blocks", "translations", "neighbors"}}, ensure_ascii=False)
    intro = next((e for e in sample["extracts"] if e.get("kind") == "introduction"),
                 {"points": [{"block_id": b["id"], "text": b["text"]} for b in sample["blocks"]]})
    taxonomy = Taxonomy.load(Path(__file__).parents[1] / "taxonomy.toml").prompt_table()
    cases = {
        "title": (TITLE_SCHEMA, TITLE_SYSTEM, '标题：' + sample['title']),
        "preview": (preview.PREVIEW_SCHEMA, preview.SYSTEM, material + "\n引言节选：\n" + rendered),
        "extract": (extract.MODULE_SCHEMA, extract.SYSTEM, "章节：Introduction（类别 introduction）\n原文片段：\n" + rendered),
        "summarize": (summarize.SUMMARY_SCHEMA, summarize.SYSTEM, material),
        "glossary": (glossary.GLOSSARY_SCHEMA, glossary.SYSTEM, material + "\n原文：\n" + rendered),
        "translate": (translator.TRANSLATE_SCHEMA, translator.BASE_SYSTEM, "术语表：\n" + json.dumps(sample["glossary"], ensure_ascii=False) + "\n待翻译原文（每行以 [block_id] 开头）：\n" + rendered),
        "repair": (translator.REPAIR_SCHEMA, translator.BASE_SYSTEM, "以下块的当前译文为空（empty），请修复，保留所有数字、引用和关系符：\n" + rendered),
        "term_review": (term_review.REVIEW_SCHEMA, term_review.SYSTEM, material + "\n当前译文：\n" + json.dumps({b["id"]: sample["translations"].get(b["id"], "") for b in sample["blocks"]}, ensure_ascii=False)),
        "classify": (classifier.CLASSIFY_SCHEMA, classifier.SYSTEM, "分类体系：\n" + taxonomy + "\n" + material),
        # Reserved task: the same tag/classification contract until the pipeline enables it.
        "curate": (classifier.CLASSIFY_SCHEMA, classifier.SYSTEM, "请整理标签与分类。\n分类体系：\n" + taxonomy + "\n" + material),
        "assess": (evaluate.ASSESS_SCHEMA, evaluate.SYSTEM, material),
        "motivation": (motivation.MOTIVATION_SCHEMA, motivation.SYSTEM,
                       json.dumps({"title": sample["title"], "summary": sample["summary"], "introduction": intro}, ensure_ascii=False)),
        "motivation_tree": (tree.TREE_SCHEMA, tree.SYSTEM, json.dumps({"new_papers": [{"id": sample["paper"], "title": sample["title"],
                "motivation": sample["motivation"].get("motivation", ""), "gap": sample["motivation"].get("gap", ""),
                "year": sample.get("year")}], "candidates": sample["neighbors"], "cites_pairs": []}, ensure_ascii=False)),
    }
    return cases


def _check_reply(task, reply, schema, blocks):
    # Inspect wire JSON too: backend normalisation must not hide a bad envelope.
    parsed = extract_json(reply.text)
    errors = validate_schema(parsed, schema)
    if not isinstance(parsed, dict):
        errors.append("回复不是 JSON 对象")
    elif task in {"title", "preview", "extract", "summarize", "motivation"}:
        fields = {"title": ("title_zh",), "preview": ("one_line",), "extract": ("digest",),
                  "summarize": ("one_line", "tldr"), "motivation": ("motivation",)}[task]
        if any(not isinstance(parsed.get(key), str) or not parsed[key].strip() for key in fields):
            errors.append("必要的正文内容为空")
        if task == 'title':
            from carrel.titles import title_issues
            errors.extend(title_issues(parsed.get('title_zh'), reply.user_prompt.removeprefix('标题：')))
    if reply.truncated:
        errors.append("回复被截断")
    if reply.substituted:
        errors.append("实际回复模型与请求模型不同")
    if reply.tokens_in <= 0 or reply.tokens_out <= 0 or reply.thinking_tokens > reply.tokens_out:
        errors.append("token 用量缺失或不合理")
    if reply.stop_reason not in {"stop", "end_turn", "completed", "stop_sequence"}:
        errors.append(f"异常停止原因：{reply.stop_reason}")
    coverage, hard = None, None
    if task in {"translate", "repair"} and isinstance(parsed, dict):
        values = parsed.get("translations", [])
        table = {item.get("block_id"): item.get("text", "") for item in values if isinstance(item, dict)} if isinstance(values, list) else {}
        table = {k: v if isinstance(v, str) else "" for k, v in table.items()}
        coverage = sum(bool(table.get(b["id"], "").strip()) for b in blocks) / len(blocks)
        if len(table) != len(values) or set(table) != {b["id"] for b in blocks}:
            errors.append("翻译块 ID 重复、遗漏或包含额外 ID")
        issues = [issue for b in blocks for issue in check_block(block_id=b["id"], source=b["text"], target=table.get(b["id"], ""), translate_required=True)]
        hard = sum(i.check in HARD_CHECKS for i in issues)
        if coverage < 1 or hard:
            errors.append(f"翻译覆盖率 {coverage:.0%}，硬 QA 问题 {hard}")
        if any(not any('\u4e00' <= c <= '\u9fff' for c in table.get(b["id"], "")) for b in blocks):
            errors.append("译文缺少中文，可能只是复制原文")
    return errors, coverage, hard


def verify_provider(config, provider, *, paper_id="", baseline_provider=None, progress=None, router_factory=ProviderRouter):
    """One call per task, plus the same real sample on the configured GLM baseline."""
    if not provider.configured or not provider.model:
        raise ValueError("请先配置提供方的 API 地址、密钥和模型名")
    atomic_write_json(report_path(config.data_dir, provider.id), {"fingerprint": fingerprint(provider), "passed": False, "status": "running"})
    sample = load_sample(config.data_dir, paper_id)
    cases = task_cases(sample)
    candidate = router_factory({provider.id: provider}, recommended_routes(provider),
                               fallback_config=config.model, default_provider=provider.id)
    baseline = router_factory(fallback_config=config.model)
    is_glm = bool(config.model.api_key) and all(config.model.for_task(t)[0].lower().startswith("glm") for t in MODEL_TASKS)
    if baseline_provider is not None:
        is_glm = baseline_provider.configured and baseline_provider.model.lower().startswith("glm")
        baseline = router_factory({baseline_provider.id: baseline_provider}, recommended_routes(baseline_provider),
                                  fallback_config=config.model, default_provider=baseline_provider.id)
    if not is_glm:
        raise ValueError("逐任务验证需要可用的 GLM 基线。请将旧 [model] 配置为 GLM，或使用脚本 --baseline-provider 选择已保存的 GLM 提供方。")
    rows = []
    for task in MODEL_TASKS:
        schema, system, user = cases[task]
        row = {"task": task, "passed": False}
        for label, backend in (("candidate", candidate), ("baseline", baseline)):
            try:
                reply = backend.raw_structured(schema=schema, system=system, user=user, task=task)
                errors, coverage, hard = _check_reply(task, reply, schema, sample["blocks"])
                row[label] = {"passed": not errors, "errors": errors, "coverage": coverage, "hard_qa": hard,
                              **{k: v for k, v in reply.accounting().items() if k != "_trace"},
                              "response": provider.redact(reply.text)}
            except Exception as exc:
                reply = getattr(exc, "reply", None)
                row[label] = {"passed": False, "errors": [provider.redact(str(exc))],
                              "response": provider.redact(reply.text) if reply else ""}
        row["passed"] = row["candidate"]["passed"]
        if task in {"translate", "repair"}:
            a, b = row["candidate"], row["baseline"]
            row["passed"] = bool(a["passed"] and b["passed"] and a["coverage"] >= b["coverage"] and a["hard_qa"] <= b["hard_qa"])
        rows.append(row)
        if progress:
            progress(task, len(rows), len(MODEL_TASKS))
    baseline_passed = is_glm and all(r["baseline"]["passed"] for r in rows)
    report = {"provider": provider.id, "fingerprint": fingerprint(provider), "version": VERIFY_VERSION,
              "baseline": baseline_provider.id if baseline_provider else "legacy",
              "paper": sample["paper"], "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "tasks": rows, "baseline_passed": baseline_passed,
              "passed": bool(baseline_passed and all(r["passed"] for r in rows))}
    # Even failures replace earlier passes; changed config invalidates the fingerprint.
    atomic_write_json(report_path(config.data_dir, provider.id), report)
    return report
