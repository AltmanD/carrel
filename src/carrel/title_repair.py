"""Read-only audit and backed-up, resumable title repair for an existing library."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from carrel.store.paths import acquire_lock, release_lock, paper_dir, read_json, atomic_write_json
from carrel.titles import audit_title, ensure_title, recover_title, current_record, title_issues


def audit_library(data_dir: Path, paper_ids=()) -> dict:
    """Opening a read-only connection also avoids schema migrations during audit."""
    with sqlite3.connect((data_dir / 'app.db').resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute('SELECT * FROM papers ORDER BY id')]
    selected = set(paper_ids)
    missing = selected - {row['id'] for row in rows}
    if missing:
        raise ValueError('论文不存在：' + ', '.join(sorted(missing)))
    entries = [audit_title(row, paper_dir(data_dir, row['id'])) for row in rows
               if not selected or row['id'] in selected]
    return {'papers': len(entries), 'candidates': sum(bool(e['issues']) for e in entries),
            'repairable': sum(e['repairable'] for e in entries), 'items': entries}


def repair_library(config, *, paper_ids=(), resume: Path | None = None, backend=None) -> dict:
    """Mutations require the same process lock as serve/import; no parser is used."""
    from carrel.store.db import Database
    from carrel.model.router import ProviderRouter
    from carrel.runtime.limits import Runtime

    data_dir = config.data_dir.resolve()
    lock = acquire_lock(data_dir)
    if lock is None:
        raise ValueError('论文库正在使用，请先停止 Carrel 服务再修复；只读审计可在服务运行时执行。')
    db = None
    try:
        if resume:
            backup = resume.resolve()
            report = read_json(backup / 'report.json', None, strict=True)
            if not isinstance(report, dict) or report.get('data_dir') != str(data_dir):
                raise ValueError('恢复报告不属于当前论文库')
            if paper_ids:
                raise ValueError('恢复时使用报告中原有的论文范围，不能同时指定 --paper')
            if not (backup / 'app.db').is_file():
                raise ValueError('缺少原数据库备份，无法恢复修复任务')
        else:
            audit = audit_library(data_dir, paper_ids)
            backup = data_dir / '_backup' / ('titles-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            backup.mkdir(parents=True, exist_ok=False)
            with sqlite3.connect((data_dir / 'app.db').as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(backup / 'app.db') as destination:
                    source.backup(destination)
            report = {'data_dir': str(data_dir), 'backup': str(backup),
                      'items': [dict(item, outcome='pending' if item['repairable'] else 'review')
                                for item in audit['items'] if item['issues']]}
            for item in report['items']:
                base = paper_dir(data_dir, item['paper_id'])
                for name in ('preview.json', 'title.json', 'title.pending.json'):
                    path = base / name
                    if path.is_file():
                        target = backup / path.relative_to(data_dir)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)
            atomic_write_json(backup / 'report.json', report)
        db = Database(data_dir / 'app.db')
        backend = backend or ProviderRouter.from_config(config, caps_path=data_dir / 'model_caps.json')
        if hasattr(backend, 'configure_runtime'):
            backend.configure_runtime(Runtime(config))
        for item in report['items']:
            if item['outcome'] != 'pending':
                continue
            pid = item['paper_id']
            base = paper_dir(data_dir, pid)
            # A completed title journal is durable even if this report was not.
            recover_title(db, pid, base)
            row = db.get_paper(pid)
            saved = read_json(base / 'title.json', {}) or {}
            original_record = read_json(backup / 'papers' / base.name / 'title.json', {}) or {}
            same_source = row and row.get('title') == item['source_title']
            recovered = (same_source and current_record(saved, item['source_title'])
                         and row.get('title_zh') == saved.get('title_zh')
                         and saved != original_record
                         and saved.get('status') in ('accepted', 'failed', 'source_missing')
                         and (saved.get('status') != 'accepted' or not title_issues(
                             saved.get('title_zh'), item['source_title'], human=saved.get('origin') == 'human')))
            if not same_source or (row.get('title_zh') != item['old_title_zh'] and not recovered):
                item['outcome'] = 'conflict'
            else:
                result = saved if resume and recovered else ensure_title(config, db, backend, pid, retry=not bool(resume))
                item['outcome'] = result['status']
                item['new_title_zh'] = result.get('title_zh', '')
                item['run_ids'] = result.get('run_ids', [])
                item['remaining_issues'] = result.get('issues', [])
                item['usage'] = [dict(r) for run_id in item['run_ids'] for r in db.query(
                    'SELECT id, tokens_in, tokens_out, cost_usd, status FROM model_runs WHERE id=?', (run_id,))]
            atomic_write_json(backup / 'report.json', report)
        report['counts'] = {status: sum(i['outcome'] == status for i in report['items'])
                            for status in sorted({i['outcome'] for i in report['items']})}
        atomic_write_json(backup / 'report.json', report)
        return report
    finally:
        if db:
            db.close()
        release_lock(lock)
