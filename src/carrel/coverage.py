"""Derive capabilities from valid artifacts, never from a requested preset."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from carrel.models import DocModel
from carrel.profile import selected_section, section_identity, skipped_sections
from carrel.store.paths import read_json


def paper_coverage(base, fp=None) -> set[str]:
    base = Path(base)
    files = [base / name for name in ('doc.json', 'sections.json', 'summary.json', 'classification.json',
              'assessment.json', 'motivation.json', 'relations.json', 'extract_skipped.json')]
    files += list((base / 'modules').glob('*.json')) + list((base / 'translation').glob('*.json'))
    stamp = []
    for path in sorted(files):
        try:
            stat = path.stat()
            stamp.append((str(path.relative_to(base)), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return set(_cached_coverage(str(base.resolve()), tuple(stamp), fp))


@lru_cache(maxsize=512)
def _cached_coverage(base, stamp, fp):
    return frozenset(_compute_coverage(Path(base), fp))


def _object(path):
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def _compute_coverage(base, fp):
    result = set()
    classification = read_json(base / 'classification.json', {}) or {}
    if not isinstance(classification, dict):
        classification = {}
    if (classification.get('origin') == 'human' and
            (classification.get('categories') or classification.get('discipline'))):
        result.add('classified')
    relations = _object(base / 'relations.json')
    relations_ok = (relations and not relations.get('errors')
                    and 'failed' not in (relations.get('coverage') or {}).values())
    doc_data = read_json(base / 'doc.json', {}) or {}
    if not isinstance(doc_data, dict) or not doc_data.get('blocks'):
        if relations_ok and not fp and not relations.get('doc_fingerprint'):
            result.add('relations')
        return result
    try:
        doc = DocModel.from_dict(doc_data)
    except (TypeError, ValueError, KeyError):
        return result
    actual = doc.fingerprint()
    if fp is not None and fp != actual:
        return set()
    fp = actual
    result.add('parsed')
    sections = [s for s in _object(base / 'sections.json').get('sections', [])
                if isinstance(s, dict) and s.get('kind') and isinstance(s.get('block_ids'), list)]
    blocks = doc.blocks_by_id()
    modules = {}
    for path in sorted((base / 'modules').glob('*.json')):
        part = read_json(path, {}) or {}
        if (isinstance(part, dict) and part.get('doc_fingerprint') == fp
                and (part.get('digest') or part.get('points')) and any(
                    part.get('kind') == s['kind']
                    and part.get('title_original', '').strip() == s.get('title', '').strip()
                    and part.get('block_span') == [s['block_ids'][0], s['block_ids'][-1]]
                    for s in sections if s['block_ids'])):
            modules[path.stem] = part
    skips = skipped_sections(_object(base / 'extract_skipped.json'), sections, fp)

    def extracted(section):
        ids = [bid for bid in section.get('block_ids', []) if bid in blocks]
        span = [ids[0], ids[-1]] if ids else []
        return section_identity(section) in skips or any(
            m.get('kind') == section['kind'] and m.get('title_original', '').strip() == section.get('title', '').strip()
            and m.get('block_span') == span for m in modules.values())

    translated = []
    for path in (base / 'translation').glob('*.json'):
        part = read_json(path, {}) or {}
        if isinstance(part, dict) and part.get('doc_fingerprint') == fp:
            translated.append(part)

    def has_translation(section):
        from carrel.translate.translator import SKIP_TYPES
        ids = [bid for bid in section.get('block_ids', []) if bid in blocks and blocks[bid].type not in SKIP_TYPES]
        return any(t.get('kind') == section['kind'] and t.get('title', '').strip() == section.get('title', '').strip()
                   and all(t.get('translations', {}).get(bid) for bid in ids) for t in translated)

    for scope in ('key', 'core', 'all'):
        expected = [s for s in sections if selected_section(s, scope) and s.get('block_ids')]
        if expected and modules and all(extracted(s) for s in expected):
            result.add('extract_' + scope)
        expected = [s for s in sections if selected_section(s, scope, translation=True) and s.get('block_ids')]
        if expected and all(has_translation(s) for s in expected):
            result.add('translated_' + scope)
    # Scoped capabilities disambiguate partial/key translation on upgrade.
    if any(cap.startswith('translated_') for cap in result):
        result.add('translated')
    h = hashlib.sha256()
    for key, m in sorted(modules.items()):
        h.update(f"{key}\x1f{m.get('kind', '')}\x1f{m.get('digest', '')}\n".encode('utf-8', 'surrogatepass'))
    summary = _object(base / 'summary.json')
    if (summary.get('doc_fingerprint') == fp and summary.get('extracts_fingerprint') == h.hexdigest()[:16]
            and summary.get('one_line') and summary.get('one_line_source', 'model') == 'model'):
        result.add('summary')
    for filename, cap in (('classification', 'classified'), ('assessment', 'assessed')):
        part = read_json(base / (filename + '.json'), {}) or {}
        if isinstance(part, dict) and part.get('doc_fingerprint') == fp:
            if cap != 'classified' or part.get('categories') or part.get('discipline'):
                result.add(cap)
    from carrel.motivation.extract import reusable_motivation
    if 'summary' in result and reusable_motivation(base, fp):
        result.add('motivation')
    if relations_ok and relations.get('doc_fingerprint', fp) == fp:
        result.add('relations')
    return result


def missing_capabilities(profile, coverage):
    from carrel.profile import requires
    missing = requires(profile) - coverage
    if profile.translate:
        if 'translated_' + profile.extract in coverage:
            missing.discard('translated')
        else:
            missing.add('translated')
    return missing
