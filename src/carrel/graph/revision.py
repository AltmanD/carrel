"""Cheap write-driven invalidation for the rebuildable graph projection."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from threading import RLock

_lock = RLock()
_versions: dict[str, int] = defaultdict(int)
_paper_versions: dict[tuple[str, str], int] = defaultdict(int)
_taxonomy_versions: dict[str, int] = defaultdict(int)


def _key(data_dir: Path | str) -> str:
    return str(Path(data_dir).absolute()).casefold()


def revision(data_dir: Path | str) -> int:
    with _lock:
        return _versions[_key(data_dir)]


def touch(data_dir: Path | str) -> None:
    with _lock:
        _versions[_key(data_dir)] += 1


def paper_revision(data_dir: Path | str, directory: str) -> tuple[int, int]:
    with _lock:
        key = _key(data_dir)
        return _paper_versions[key, directory], _taxonomy_versions[key]


def artifact_written(path: Path) -> None:
    """Only finished display/coverage artifacts invalidate, never trace/progress."""
    if path.name in {'motivation_tree.json', 'taxonomy.toml'} or (path.name == 'graph_display.json' and path.parent.parent.name != 'papers'):
        touch(path.parent)
        if path.name == 'taxonomy.toml':
            with _lock:
                _taxonomy_versions[_key(path.parent)] += 1
        return
    if path.suffix == '.json' and path.parent.name in {'modules', 'translation'} and path.parent.parent.parent.name == 'papers':
        base = path.parent.parent
        touch(base.parent.parent)
        with _lock:
            _paper_versions[_key(base.parent.parent), base.name] += 1
        return
    if path.name in {'classification.json', 'summary.json', 'preview.json',
                     'glossary.json', 'title.json', 'motivation.json',
                     'assessment.json', 'sections.json', 'doc.json', 'relations.json', 'graph_display.json',
                     'extract_skipped.json', 'translate_warnings.json'}:
        if path.parent.parent.name == 'papers':
            touch(path.parent.parent.parent)
            with _lock:
                _paper_versions[_key(path.parent.parent.parent), path.parent.name] += 1
