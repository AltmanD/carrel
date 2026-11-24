"""User-authored graph labels, kept separate from analysis artifacts."""
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from carrel.store.paths import atomic_write_json, paper_dir, read_json

_lock = RLock()


def branch_names(data_dir):
    saved = read_json(Path(data_dir) / 'graph_display.json', {}) or {}
    return saved.get('branches', {}) if isinstance(saved, dict) else {}


def save_branch_name(data_dir, branch_id, label):
    if not isinstance(branch_id, str) or not branch_id.startswith('branch:'):
        raise ValueError('Invalid branch ID')
    if not isinstance(label, str) or len(label.strip()) > 60 or any(ord(c) < 32 for c in label):
        raise ValueError('Branch name must be a single line of at most 60 characters')
    label = label.strip()
    path = Path(data_dir) / 'graph_display.json'
    with _lock:
        saved = read_json(path, {}) or {}
        branches = saved.setdefault('branches', {})
        if label:
            branches[branch_id] = label
        else:
            branches.pop(branch_id, None)
        atomic_write_json(path, saved)
    return label


def display_name(data_dir, paper_id):
    saved = read_json(paper_dir(data_dir, paper_id) / 'graph_display.json', {}) or {}
    return str(saved.get('label') or '') if isinstance(saved, dict) else ''


def save_display_name(data_dir, paper_id, label):
    if not isinstance(label, str):
        raise ValueError('方法名须为文字')
    label = label.strip()
    if len(label) > 60 or any(ord(c) < 32 for c in label):
        raise ValueError('方法名最多 60 个字符，请使用单行简短名称')
    atomic_write_json(paper_dir(data_dir, paper_id) / 'graph_display.json', {
        'label': label, 'origin': 'human',
        'updated_at': datetime.now(timezone.utc).isoformat(),
    })
    return label
