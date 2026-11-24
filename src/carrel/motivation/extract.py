"""One main motivation per paper; optional, resumable and independently billed."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from carrel.model import ModelSchemaError, ModelTruncated
from carrel.model.base import call_json, pick
from carrel.models import DocModel
from carrel.store.paths import atomic_write_json, paper_dir, read_json
from carrel.store.runs import record_run

FIELDS = ("motivation", "gap", "direction", "keywords", "evidence_block_ids")
MOTIVATION_SCHEMA = {
    "type": "object",
    "properties": {
        **{key: {"type": "string"} for key in FIELDS[:3]},
        **{key: {"type": "array", "items": {"type": "string"}} for key in FIELDS[3:]},
    },
    "required": list(FIELDS), "additionalProperties": False,
}
SYSTEM = """你在整理论文的主要研究动机。只依据给定引言要点和总结，不引入外部知识。
每篇只保留一个最主要的 motivation，用一句中文说明为什么需要这项工作。
gap 是先前工作的具体不足，direction 是研究方向；
keywords 保留 2–6 个通行关键词。不能从材料确认的字段留空，不猜测。
evidence_block_ids 只能取材料中实际出现的 block_id，无证据则留空。
不把本文自身的 limitations 误认为先前工作的 gap。只输出 JSON 对象。"""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，;；、\n]+", value)
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))


def normalize_motivation(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("value"), dict):
        data = {**data, **data["value"]}
    aliases = {
        "motivation": ("main_motivation", "动机", "研究动机", "主要动机"),
        "gap": ("research_gap", "不足", "研究空白", "局限"),
        "direction": ("research_direction", "方向", "研究方向"),
        "keywords": ("key_words", "关键词"),
        "evidence_block_ids": ("block_ids", "evidence_ids", "证据块", "证据块ID"),
    }
    result = {key: (_strings if key in FIELDS[3:] else _text)(pick(data, key, *other))
              for key, other in aliases.items()}
    return result if result["motivation"] else None


def extract_motivation(backend, *, title, summary, intro_extract):
    summary = summary.to_dict() if hasattr(summary, "to_dict") else summary
    intro = intro_extract.to_dict() if hasattr(intro_extract, "to_dict") else intro_extract
    intro = intro if isinstance(intro, dict) else {}
    material = {"title": title, "summary": summary, "introduction": intro}
    reply = call_json(backend, schema=MOTIVATION_SCHEMA, system=SYSTEM,
                      user=json.dumps(material, ensure_ascii=False), max_tokens=1800, task="motivation")
    if reply.truncated:
        raise ModelTruncated("动机抽取回复被截断", reply=reply)
    result = normalize_motivation(reply.data)
    if result is None:
        raise ModelSchemaError("未取得有效的主要动机", reply=reply)
    valid = {p.get("block_id") for p in intro.get("points", []) if isinstance(p, dict)}
    if valid:
        result["evidence_block_ids"] = [bid for bid in result["evidence_block_ids"] if bid in valid]
    return result, reply.accounting()


def document_fingerprint(base) -> str:
    doc = read_json(base / "doc.json", {})
    return DocModel.from_dict(doc).fingerprint() if isinstance(doc, dict) and doc.get("blocks") else ""


def motivation_inputs(base, fingerprint=None):
    """Fingerprint the actual sources, including schema/prompt version."""
    fp = fingerprint if fingerprint is not None else document_fingerprint(base)
    saved_summary = read_json(base / "summary.json", {}) or {}
    summary = {k: saved_summary.get(k) for k in ("one_line", "argument_chain", "limitations")}
    intro, extracts = {"digest": "", "points": []}, {}
    for path in sorted((base / "modules").glob("*.json")):
        part = read_json(path, {}) or {}
        if not isinstance(part, dict) or path.stem.startswith("_"):
            continue
        if part.get("doc_fingerprint") not in (None, fp):
            continue
        extracts[path.stem] = {k: v for k, v in part.items() if k not in ("run_id", "doc_fingerprint")}
        if part.get("kind") == "introduction" or path.stem.startswith("introduction"):
            intro["digest"] += str(part.get("digest") or "") + "\n"
            intro["points"].extend(p for p in (part.get("points") or []) if isinstance(p, dict))
    material = {"doc": fp, "extracts": extracts, "summary": saved_summary, "version": "motivation-v1"}
    mot_fp = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return fp, mot_fp, summary, intro


def reusable_motivation(base, fingerprint=None):
    saved = read_json(base / "motivation.json", {})
    mot_fp = motivation_inputs(base, fingerprint)[1]
    return saved if isinstance(saved, dict) and saved.get("input_fingerprint") == mot_fp and normalize_motivation(saved) else None


def ensure_motivation(config, db, backend, paper, *, force=False, retry=False, fingerprint=None):
    """Optional work: absorb model errors, record spend, back off after two failures."""
    base = paper_dir(config.data_dir, paper["id"])
    fp, mot_fp, summary, intro = motivation_inputs(base, fingerprint)
    saved = read_json(base / "motivation.json", {})
    if not force and isinstance(saved, dict) and saved.get("input_fingerprint") == mot_fp and normalize_motivation(saved):
        return saved
    attempt_path = base / "_motivation_attempts.json"
    attempt = read_json(attempt_path, {}) or {}
    count = int(attempt.get("n", 0)) if attempt.get("fp") == mot_fp else 0
    if not force and not retry and count >= 2:
        return None
    # Points-only introductions are valid too; an absent/empty introduction is not.
    if not fp or not (intro["digest"].strip() or intro["points"]):
        if attempt.get("fp") != mot_fp or attempt.get("status") != "skipped":
            reason = "缺少引言摘要或要点，跳过动机抽取"
            record_run(config, db, backend, paper_id=paper["id"], task="motivation", status="skipped", error_code=reason)
            atomic_write_json(attempt_path, {"fp": mot_fp, "n": 0, "status": "skipped", "error": reason})
        return None
    try:
        result, meta = extract_motivation(backend, title=paper.get("title") or paper["id"],
                                          summary=summary, intro_extract=intro)
        doc = read_json(base / "doc.json", {})
        valid = {b.get("id") for b in doc.get("blocks", []) if isinstance(b, dict)}
        result["evidence_block_ids"] = [bid for bid in result["evidence_block_ids"] if bid in valid]
    except Exception as exc:
        reply = getattr(exc, "reply", None)
        record_run(config, db, backend, paper_id=paper["id"], task="motivation",
                   status="failed", schema_ok=0, error_code=str(exc)[:200],
                   meta=reply.accounting() if reply else {})
        atomic_write_json(attempt_path, {"fp": mot_fp, "n": count + 1, "status": "failed", "error": str(exc)[:200]})
        return None
    result.update(origin="llm", doc_fingerprint=fp, input_fingerprint=mot_fp, run_id=record_run(
        config, db, backend, paper_id=paper["id"], task="motivation", meta=meta))
    atomic_write_json(base / "motivation.json", result)
    attempt_path.unlink(missing_ok=True)
    return result
