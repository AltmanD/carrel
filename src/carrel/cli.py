"""Command line entry point: serve and import."""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

from carrel.config import load_config
from carrel.store.db import Database
from carrel.store.paths import acquire_lock, release_lock


def _build(args) -> tuple:
    config = load_config(data_dir=args.data_dir, config_file=args.config)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.data_dir / "app.db")
    return config, db


def cmd_serve(args) -> int:
    import uvicorn

    from carrel.web.app import create_app

    from carrel.config import data_directory
    lock = acquire_lock(data_directory(args.data_dir))
    if lock is None:
        print("论文库正在使用，请在网页端导入，或先停止服务。", file=sys.stderr)
        return 1

    db = None
    try:
        config, db = _build(args)
        app = create_app(config, db)
        port = args.port or config.port
        url = f"http://{config.host}:{port}"
        print(f"Carrel running at {url}")
        print(f"data directory: {config.data_dir}")
        if not config.mineru.enabled:
            print("warning: MINERU_TOKEN is not set; imports will fail at the parse stage")
        if args.open:
            webbrowser.open(url)
        uvicorn.run(app, host=config.host, port=port, log_level="info")
    finally:
        if db:
            db.close()
        release_lock(lock)
    return 0


def _run_import_batch(args, inputs):
    from carrel.config import data_directory
    from carrel.scheduler import ImportScheduler
    lock = acquire_lock(data_directory(args.data_dir))
    if lock is None:
        print('论文库正在使用，请在网页端导入，或先停止服务。', file=sys.stderr)
        return 1
    db = scheduler = None
    interrupted = False
    try:
        config, db = _build(args)
        from carrel.profile import parse_profile, default_profile, Profile
        requested = parse_profile(args.depth) if getattr(args, 'depth', None) else default_profile(config.data_dir)
        values = requested.to_dict()
        for flag in ('translate', 'classify', 'assess', 'motivation', 'relations'):
            if getattr(args, 'no_' + flag, False):
                values[flag] = False
        requested = Profile(**values)
        scheduler = ImportScheduler(config, db, workers=getattr(args, 'workers', None))
        batch_id = scheduler.resume(args.resume) if getattr(args, 'resume', '') else scheduler.submit(inputs, force=args.force, profile=requested)
        print(f'批次 {batch_id} · {scheduler.workers} 篇并行。中断后可用 batch --resume {batch_id} 继续。', flush=True)
        shown = set()
        while True:
            batch = scheduler.status(batch_id)
            completed = sorted((e for e in batch['items'] if e['status'] in ('succeeded', 'failed', 'partial', 'ready_with_warning')),
                               key=lambda e: e.get('finished_at', 0))
            for entry in completed:
                if entry['job_id'] not in shown:
                    shown.add(entry['job_id'])
                    print(f"[{len(shown)}/{len(batch['items'])}] {entry['input']} -> {entry['status']} {entry['error_code']}", flush=True)
            if batch['finalized']:
                print(f"完成 · {batch['counts']} · 重试 {batch['retries']} 次 · {batch['wall_s']:.1f}s", flush=True)
                if batch['warning']:
                    print(batch['warning'], file=sys.stderr)
                return 2 if any(e['status'] == 'failed' for e in batch['items']) else 0
            time.sleep(.25)
    except KeyboardInterrupt:
        interrupted = True
        print('已保留批次清单和已完成阶段；下次使用上方 batch --resume 命令继续。', file=sys.stderr)
        return 130
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if scheduler:
            scheduler.shutdown(wait=not interrupted)
        if not interrupted:
            if db:
                db.close()
            release_lock(lock)
        # On Ctrl+C keep the OS lock until process exit; daemon workers cannot
        # overlap a new writer while an in-flight request is still unwinding.


def cmd_import(args) -> int:
    return _run_import_batch(args, args.inputs)


def cmd_sync(args) -> int:
    import json

    from carrel.sync.apply import apply_scan
    from carrel.sync.scanner import scan
    from carrel.config import data_directory

    try:
        if args.report:
            print(Path(args.report).read_text(encoding="utf-8"))
            return 0
        if not args.directory:
            raise ValueError("A local directory is required")
        data_dir = data_directory(args.data_dir)
        result = scan(Path(args.directory), collections_path=Path(args.collections) if args.collections else data_dir / "collections.toml")
        if args.dry_run:
            report = result.to_dict()
        else:
            lock = acquire_lock(data_dir)
            if lock is None:
                print('论文库正在使用，请在网页端同步，或先停止服务。', file=sys.stderr)
                return 1
            db = None
            try:
                db = Database(data_dir / "app.db")
                report = apply_scan(result, db, data_dir)
            finally:
                if db:
                    db.close()
                release_lock(lock)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 2


def cmd_health(args) -> int:
    config, db = _build(args)
    from carrel.model.router import ProviderRouter

    print(f"data directory : {config.data_dir}")
    print(f"mineru token   : {'configured' if config.mineru.enabled else 'MISSING'}")
    print(f"sciverse token : {'configured' if config.sciverse.enabled else 'not set'}")
    print(f"model protocol : {config.model.protocol}")
    print(f"model name     : {config.model.name}")
    print(f"model thinking : {config.model.thinking}")
    print(f"model base_url : {config.model.base_url or '(default)'}")
    print(f"model api key  : {'configured' if config.model.api_key else 'MISSING'}")
    overrides = {t: config.model.for_task(t) for t in config.model.by_task}
    for task, (name, thinking, ceiling) in sorted(overrides.items()):
        print(f"  by_task.{task:<12}: {name} thinking={thinking} max_tokens={ceiling}")
    print(f"papers         : {len(db.list_papers())}")
    rc = 0
    try:
        router = ProviderRouter.from_config(config, caps_path=config.data_dir / "model_caps.json")
        print(f"default route  : {config.default_provider or 'legacy'}")
        if config.provider_error:
            print(config.provider_error, file=sys.stderr)
            rc = 2
        for pid in ["legacy", *config.providers]:
            provider = config.providers.get(pid)
            if provider:
                print(f"provider {pid}: {provider.protocol} {provider.base_url} key={'configured' if provider.configured else 'MISSING'}")
            try:
                backend = router.backend_for_provider(pid)
                # Health is an explicit probe; bypass cached capabilities.
                backend._caps.clear()
                backend._caps_path = None
                caps = backend.capabilities()
                if getattr(backend, "capability_error", ""):
                    raise RuntimeError(backend.capability_error)
                print(f"  {pid}: strict_schema={caps.strict_schema} json_mode={caps.json_mode}")
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if provider:
                    message = provider.redact(message)
                elif config.model.api_key:
                    message = message.replace(config.model.api_key, "[redacted]")
                print(f"  {pid}: probe failed ({message}); check /settings", file=sys.stderr)
                rc = 2
    finally:
        db.close()
    return rc


def format_usage(report: dict, *, title: str = "") -> str:
    """Render ``Database.usage_summary`` as a fixed-width table.

    Shared by the ``usage`` command and the benchmark script so before/after
    numbers are always read in the same layout.
    """
    def row(label: str, r: dict) -> str:
        think = int(r.get("thinking_tokens") or 0)
        out = int(r.get("tokens_out") or 0)
        share = f"{100 * think / out:3.0f}%" if out else "  -"
        return (f"{label:<12} {int(r.get('calls') or 0):>5} {int(r.get('tokens_in') or 0):>10} "
                f"{out:>10} {think:>9} {share:>5} {int(r.get('truncated') or 0):>5} "
                f"{int(r.get('failed') or 0):>4} {int(r.get('accepted') or 0):>4}/{int(r.get('rejected') or 0):<4}"
                f"${float(r.get('cost_usd') or 0):>9.4f}")

    lines = []
    if title:
        lines.append(title)
    lines.append(f"{'task':<12} {'calls':>5} {'input':>10} {'output':>10} {'thinking':>9} "
                 f"{'thk%':>5} {'trunc':>5} {'fail':>4} {'acc/rej':>9} {'cost':>10}")
    for r in report.get("by_task", []):
        lines.append(row(str(r.get("task") or ""), r))
    total = dict(report.get("total") or {})
    lines.append("-" * len(lines[-1]))
    lines.append(row("total", total))
    lines.append(f"papers: {total.get('papers', 0)}")
    if int(total.get("substituted") or 0):
        lines.append(f"WARNING: {int(total['substituted'])} call(s) were answered by a different model "
                     f"than requested — see the by-model breakdown; cost is billed at the answering model's rate")
    by_model = report.get("by_model") or []
    if report.get("by_provider"):
        lines.append("")
        lines.append("by provider:")
        for r in report["by_provider"]:
            lines.append(row(str(r.get("provider") or "legacy"), r))
    models = sorted({str(r.get("model") or "") for r in by_model})
    if len(models) > 1 or int(total.get("substituted") or 0):
        lines.append("")
        lines.append("by model:")
        for r in by_model:
            lines.append(row(f"{r.get('task')}/{r.get('model')}"[:12], r))
    return "\n".join(lines)


def cmd_usage(args) -> int:
    """Model spend, grouped by task — the number the cost work is measured by."""
    import json

    config, db = _build(args)
    report = db.usage_summary(paper_id=args.paper or None, since=args.since or None)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        scope = f"paper {args.paper}" if args.paper else str(config.data_dir)
        if args.since:
            scope += f" since {args.since}"
        print(format_usage(report, title=f"model usage — {scope}"))
    db.close()
    return 0


def cmd_batch(args) -> int:
    import csv
    import json
    inputs = []
    if args.resume:
        if args.dry_run:
            from carrel.config import data_directory
            from carrel.store.paths import read_json
            import re
            if not re.fullmatch(r'batch-[a-f0-9]{12}', args.resume):
                print('invalid batch id', file=sys.stderr)
                return 2
            print(json.dumps(read_json(data_directory(args.data_dir) / '_batches' / (args.resume + '.json'), {}), ensure_ascii=False, indent=2))
            return 0
        return _run_import_batch(args, inputs)
    path = Path(args.csv or '')
    if path.is_dir() and args.csv:
        from carrel.sync.scanner import scan
        from carrel.config import data_directory
        result = scan(path, collections_path=data_directory(args.data_dir) / 'collections.toml')
        inputs = [item["source_path"] for item in result.papers]
    elif path.is_file():
        with path.open(encoding='utf-8-sig', newline='') as fh:
            inputs = [raw for row in csv.DictReader(fh) if (raw := _pick_input(row, prefer=args.prefer))]
    else:
        print('请提供 CSV、PDF 目录，或 --resume 批次 ID。', file=sys.stderr)
        return 2
    if args.limit:
        inputs = inputs[:args.limit]
    if args.dry_run:
        print(json.dumps({'count': len(inputs), 'inputs': inputs}, ensure_ascii=False, indent=2))
        return 0
    return _run_import_batch(args, inputs)


def _pick_input(row: dict[str, str], prefer: str) -> str:
    order = {
        "local": ("local_path", "arxiv_link", "pdf_direct_link", "hf_link"),
        "arxiv": ("arxiv_link", "local_path", "pdf_direct_link", "hf_link"),
        "hf": ("hf_link", "arxiv_link", "local_path", "pdf_direct_link"),
        "pdf": ("pdf_direct_link", "arxiv_link", "local_path", "hf_link"),
    }[prefer]
    for key in order:
        value = (row.get(key) or "").strip()
        if not value:
            continue
        if key == "local_path" and not Path(value).is_file():
            continue
        return value
    return ""


def cmd_titles(args) -> int:
    import json
    import sqlite3
    from carrel.config import data_directory
    from carrel.title_repair import audit_library, repair_library
    try:
        if args.resume and not args.repair:
            raise ValueError('--resume 需要同时指定 --repair')
        if args.repair:
            config = load_config(data_dir=args.data_dir, config_file=args.config)
            result = repair_library(config, paper_ids=args.paper, resume=Path(args.resume) if args.resume else None)
        else:
            result = audit_library(data_directory(args.data_dir), args.paper)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carrel", description="Personal paper reading desk")
    parser.add_argument("--data-dir", default=None, help="knowledge base directory")
    parser.add_argument("--config", default=None, help="path to a TOML config file")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="start the local web app")
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--open", action="store_true", help="open the browser")
    p_serve.set_defaults(func=cmd_serve)

    p_import = sub.add_parser("import", help="process one or more inputs")
    p_import.add_argument("inputs", nargs="+")
    p_import.add_argument("--force", action="store_true")
    p_import.set_defaults(func=cmd_import)

    p_add = sub.add_parser("add", help="alias for import")
    p_add.add_argument("inputs", nargs="+")
    p_add.add_argument("--force", action="store_true")
    p_add.set_defaults(func=cmd_import)

    p_batch = sub.add_parser("batch", help="process every row of a CSV")
    p_batch.add_argument("csv", nargs="?", help="CSV file or local PDF directory")
    p_batch.add_argument("--limit", type=int, default=0)
    p_batch.add_argument("--prefer", choices=["local", "arxiv", "hf", "pdf"],
                         default="local", help="which column to prefer")
    p_batch.add_argument('--workers', '--jobs', dest='workers', type=int, default=None, help='parallel papers (default: concurrency.papers)')
    p_batch.add_argument('--resume', default='', help='resume a saved batch ID')
    p_batch.add_argument('--dry-run', action='store_true', help='list inputs without modifying the library')
    p_batch.add_argument("--force", action="store_true")
    p_batch.set_defaults(func=cmd_batch)
    for command in (p_import, p_add, p_batch):
        command.add_argument('--depth', choices=('indexed', 'skim', 'notes', 'full'), help='processing depth (default: saved preference, initially skim)')
        for flag in ('translate', 'classify', 'assess', 'motivation', 'relations'):
            command.add_argument('--no-' + flag, action='store_true', help='skip ' + flag + ' for new imports')

    p_health = sub.add_parser("health", help="show configuration and probe the model")
    p_health.set_defaults(func=cmd_health)

    p_sync = sub.add_parser("sync", help="sync local PDF metadata, reading collections and notes (no model calls)")
    p_sync.add_argument("directory", nargs="?")
    p_sync.add_argument("--dry-run", action="store_true", help="preview only; do not write files or database")
    p_sync.add_argument("--collections", help="path to collection mapping TOML")
    p_sync.add_argument("--report", help="print a previous sync report")
    p_sync.add_argument("--skip-parse", action="store_true", help="compatibility flag; sync always skips parsing")
    p_sync.set_defaults(func=cmd_sync)

    p_usage = sub.add_parser("usage", help="model token and cost totals by task")
    p_usage.add_argument("--paper", default="", help="restrict to one paper id")
    p_usage.add_argument("--since", default="", help="ISO timestamp lower bound (created_at)")
    p_usage.add_argument("--json", action="store_true", help="machine-readable output")
    p_usage.set_defaults(func=cmd_usage)

    p_titles = sub.add_parser('titles', help='audit Chinese titles; repair only when explicitly requested')
    p_titles.add_argument('--paper', action='append', default=[], help='restrict to a paper ID; repeatable')
    p_titles.add_argument('--repair', action='store_true', help='back up and repair invalid model titles (stop server first)')
    p_titles.add_argument('--resume', help='resume from a previous title backup directory')
    p_titles.set_defaults(func=cmd_titles)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
