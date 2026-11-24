"""Durable, advisory MinerU page accounting; no parser substitution."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import logging
import time

from carrel.runtime.locks import cache_locks, path_key
from carrel.store.paths import atomic_write_json, read_json


class MinerUQuota:
    def __init__(self, root, limit=1000, behavior='warn', stop_event=None):
        self.root, self.limit, self.behavior = Path(root), limit, behavior
        self.stop_event = stop_event

    def path(self):
        return self.root / '_quota' / f'mineru-{datetime.now():%Y-%m-%d}.json'

    def snapshot(self):
        path = self.path()
        with cache_locks.hold(('quota', path_key(path))):
            value = read_json(path, {})
        return dict(pages=value.get('pages', 0), soft_limit=self.limit,
                    over_limit=value.get('pages', 0) >= self.limit, behavior=self.behavior)

    def reserve(self, pdf, progress=None):
        # pypdf reads the page tree only; full document parsing remains MinerU.
        from pypdf import PdfReader
        try:
            pages = len(PdfReader(str(pdf), strict=False).pages)
        except Exception:
            pages = 1
        while True:
            path = self.path()
            with cache_locks.hold(('quota', path_key(path))):
                # An unreadable existing counter must not be mistaken for a
                # fresh day and overwrite already reserved pages.
                value = read_json(path, {'pages': 0, 'submissions': 0}, strict=True)
                over = value['pages'] + pages > self.limit
                # One oversized document can still progress on an empty day.
                if not over or self.behavior == 'warn' or value['pages'] == 0:
                    value.update(pages=value['pages'] + pages, submissions=value.get('submissions', 0) + 1)
                    atomic_write_json(path, value)
                    if over:
                        message = 'MinerU 今日提交页数超过高优先级额度，解析可能变慢。'
                        logging.getLogger(__name__).warning(message)
                        if progress:
                            progress('quota_warning', {'message': message, 'pages': value['pages']})
                    return pages
            if progress:
                progress('quota_wait', {'message': '今日 MinerU 页数额度已用完，等待次日继续。'})
            tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), datetime.min.time())
            delay = min(60, max(1, (tomorrow - datetime.now()).total_seconds()))
            if self.stop_event:
                from carrel.runtime.context import RunInterrupted
                if self.stop_event.wait(delay):
                    raise RunInterrupted()
            else:
                time.sleep(delay)
