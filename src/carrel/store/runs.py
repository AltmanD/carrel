"""Shared model run accounting for paper and library jobs."""
from __future__ import annotations

import time
import uuid
from typing import Any

from carrel.store.paths import atomic_write_json, paper_dir


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_run(config, db, backend, *, paper_id: str, task: str, module: str = "",
                status: str = "ok", meta: dict[str, Any] | None = None,
                error_code: str = "", schema_ok: int = 1, qa_ok: int = 1,
                retry_of: str = "") -> str:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    meta = dict(meta or {})
    # Callers may attach the actual prompts and raw response under `_trace`;
    # persist them next to the run row so the detail page can show the real
    # interaction instead of a summary of it.
    trace = meta.pop("_trace", None)
    root = config.data_dir / "_library" if paper_id.startswith("@") else paper_dir(config.data_dir, paper_id)
    trace_dir = root / "runs" / run_id
    accepted = meta.get("accepted")
    provider = meta.get("provider") or (backend.provider_for(task) if hasattr(backend, "provider_for") else "legacy")
    model = meta.get("model") or meta.get("requested_model") or config.model.name
    db.record_run({
        "id": run_id,
        "paper_id": paper_id,
        "task": task,
        "module": module,
        "status": status,
        "backend": f"{provider}:{meta.get('requested_model') or model}",
        "provider": provider,
        # The model that actually answered: per-task routing may differ
        # from the configured default, and the usage report groups by it.
        "model": model,
        "tokens_in": int(meta.get("tokens_in") or 0),
        "tokens_out": int(meta.get("tokens_out") or 0),
        "duration_ms": int(meta.get("duration_ms") or 0),
        "schema_ok": schema_ok,
        "qa_ok": qa_ok,
        "retry_of": retry_of,
        "trace_dir": str(trace_dir),
        "error_code": error_code,
        "created_at": _now(),
        "thinking_tokens": int(meta.get("thinking_tokens") or 0),
        "cache_read_tokens": int(meta.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(meta.get("cache_write_tokens") or 0),
        "stop_reason": str(meta.get("stop_reason") or ""),
        "max_tokens": int(meta.get("max_tokens") or 0),
        "thinking": str(meta.get("thinking") or ""),
        "truncated": 1 if meta.get("truncated") else 0,
        "accepted": -1 if accepted is None else int(bool(accepted)),
        "cost_usd": float(meta.get("cost_usd") or 0.0),
        "requested_model": str(meta.get("requested_model") or ""),
    })
    if trace:
        atomic_write_json(trace_dir / "trace.json", {
            "run_id": run_id,
            "task": task,
            "module": module,
            "status": status,
            "provider": provider,
            "request": {"system": trace.get("system", ""),
                        "user": trace.get("user", ""),
                        # Enough of the real payload to audit cost decisions
                        # without re-deriving them from config history.
                        "model": meta.get("model", ""),
                        "max_tokens": meta.get("max_tokens", 0),
                        "thinking": meta.get("thinking", "")},
            "response": trace.get("response", {}),
            "response_text": trace.get("text", ""),
            "accounting": {k: meta.get(k) for k in (
                "tokens_in", "tokens_out", "thinking_tokens", "cache_read_tokens",
                "cache_write_tokens", "stop_reason", "truncated", "cost_usd",
                "accepted", "before_issues", "after_issues")},
        })
    return run_id
