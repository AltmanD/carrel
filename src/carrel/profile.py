"""Requested processing work, independent of the artifacts already available."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from carrel.models import CORE_SECTIONS

SCOPES = ('none', 'key', 'core', 'all')
SCOPE_FRACTIONS = {'none': 0, 'key': .22, 'core': 1, 'all': 1.5}
KEY_SECTIONS = frozenset(('abstract', 'introduction', 'conclusion'))
LABELS = {'indexed': '收录', 'skim': '速览', 'notes': '笔记', 'full': '全文', 'custom': '自定义'}


@dataclass(frozen=True)
class Profile:
    extract: str = 'key'
    translate: bool = False
    classify: bool = True
    assess: bool = True
    motivation: bool = True
    relations: bool = True

    def __post_init__(self):
        if self.extract not in SCOPES:
            raise ValueError('extract must be none, key, core or all')
        for name in ('translate', 'classify', 'assess', 'motivation', 'relations'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be boolean')
        if self.extract == 'none' and any((self.translate, self.classify, self.assess, self.motivation)):
            object.__setattr__(self, 'extract', 'key')

    def to_dict(self):
        return asdict(self)

    @property
    def name(self):
        return next((name for name, p in PRESETS.items() if p == self), 'custom')

    @property
    def label(self):
        return LABELS[self.name]

    def union(self, other):
        return Profile(extract=max((self.extract, other.extract), key=SCOPES.index),
                       **{k: getattr(self, k) or getattr(other, k)
                          for k in ('translate', 'classify', 'assess', 'motivation', 'relations')})


PRESETS = {
    'indexed': Profile('none', False, False, False, False, False),
    'skim': Profile(),
    'notes': Profile('core'),
    'full': Profile('core', True),
}


def parse_profile(value, *, default='skim') -> Profile:
    if isinstance(value, Profile):
        return value
    if value is None or value == '':
        return PRESETS[default]
    if isinstance(value, str):
        if value in PRESETS:
            return PRESETS[value]
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError('unknown processing depth') from exc
    if not isinstance(value, dict) or set(value) != set(Profile.__dataclass_fields__):
        raise ValueError('profile must specify extract, translate, classify, assess, motivation and relations')
    return Profile(**value)


def stored_profile(paper) -> Profile:
    return parse_profile(paper.get('profile'), default='indexed' if paper.get('status') == 'indexed' else 'full')


def default_profile(data_dir: Path) -> Profile:
    from carrel.store.paths import read_json
    prefs = read_json(data_dir / 'preferences.json', {}) or {}
    return parse_profile(prefs.get('import_profile'))


def save_default(data_dir: Path, profile: Profile):
    from carrel.store.paths import read_json, atomic_write_json
    prefs = read_json(data_dir / 'preferences.json', {}) or {}
    prefs['import_profile'] = profile.to_dict()
    atomic_write_json(data_dir / 'preferences.json', prefs)


def requires(profile: Profile) -> set[str]:
    required = set()
    if profile.extract != 'none':
        required.update(('parsed', 'extract_' + profile.extract, 'summary'))
    for flag, capability in (('translate', 'translated'), ('classify', 'classified'),
                             ('assess', 'assessed'), ('motivation', 'motivation'), ('relations', 'relations')):
        if getattr(profile, flag):
            required.add(capability)
    return required


def selected_section(section, scope, *, translation=False):
    # Share this predicate with coverage: absence of an optional chapter is
    # different from a chapter that exists but has not been processed.
    import re
    kind = section.get('kind', 'unknown') if isinstance(section, dict) else section.kind
    title = section.get('title', '') if isinstance(section, dict) else section.title
    if scope == 'none' or kind in ('references', 'acknowledgments'):
        return False
    if scope != 'all' and (kind == 'appendix' or re.match(r'^(?:appendix|appendices|supplement)', title.strip(), re.I)):
        return False
    if translation and title.strip().lower() in ('front matter', 'front_matter', 'contents', 'table of contents', '目录'):
        return False
    return scope == 'all' or kind in (KEY_SECTIONS if scope == 'key' else set(CORE_SECTIONS) | {'unknown'})


def section_identity(section):
    row = section if isinstance(section, dict) else section.to_dict()
    values = [row.get('kind'), row.get('title', '').strip(), row.get('block_ids', [])]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:20]


def skipped_sections(saved, sections, fingerprint):
    if not isinstance(saved, dict) or saved.get('doc_fingerprint') != fingerprint:
        return set()
    if saved.get('schema_version') == 2:
        return set(saved.get('sections') or [])
    # Legacy skips named a kind, which cannot safely skip multiple chapters.
    rows = [s if isinstance(s, dict) else s.to_dict() for s in sections]
    kinds = [s.get('kind') for s in rows]
    return {section_identity(s) for s in rows
            if s.get('kind') in (saved.get('sections') or []) and kinds.count(s.get('kind')) == 1}


# Typical per-paper token totals; these are estimates, not a provider quote.
_DEFAULTS = {'title': 350, 'preview': 1800, 'extract': 50000, 'summarize': 9000,
             'classify': 4300, 'assess': 6500, 'motivation': 3400,
             'translate': 72000, 'glossary': 3500, 'term_review': 3000}


def task_estimates(db):
    rows = db.query("SELECT CASE WHEN r.task='repair' THEN 'translate' ELSE r.task END task, "
                    "r.paper_id, p.profile, p.status, SUM(r.tokens_in+r.tokens_out) tokens, "
                    "SUM(r.cost_usd) cost FROM model_runs r LEFT JOIN papers p ON p.id=r.paper_id "
                    "WHERE r.paper_id != '@library' GROUP BY 1, r.paper_id")
    history = {}
    for row in rows:
        task = row['task']
        # Normalize historical skim/all totals before applying the requested
        # scope, otherwise a mostly-skim library discounts the same cost twice.
        scale = 1
        if task in ('extract', 'translate', 'term_review'):
            profile = stored_profile(dict(row))
            scale = SCOPE_FRACTIONS[profile.extract] or 1
        history.setdefault(task, []).append((row['tokens'] / scale, row['cost'] / scale))
    result = {}
    for task, fallback in _DEFAULTS.items():
        samples = history.get(task, [])
        tokens = sum(row[0] for row in samples) / len(samples) if samples else fallback
        cost = sum(row[1] for row in samples) / len(samples) if samples else 0
        result[task] = {'tokens': round(tokens), 'usd': cost if cost > 0 else tokens * 2 / 1_000_000,
                        'price_source': 'history' if cost > 0 else 'reference',
                        'source': 'history' if samples else 'fallback'}
    return result


def estimate(db, profile, *, coverage=None, current=None, tasks=None):
    tasks = tasks if tasks is not None else task_estimates(db)
    covered = coverage or set()
    weights = {}
    scope_fraction = SCOPE_FRACTIONS
    expanding = profile.extract != 'none' and 'extract_' + profile.extract not in covered
    if profile.extract != 'none':
        if not covered:
            weights['preview'] = 1
            weights['title'] = 1
        if expanding:
            existing = max((scope_fraction[s] for s in SCOPES[1:] if 'extract_' + s in covered), default=0)
            weights['extract'] = max(0, scope_fraction[profile.extract] - existing)
        if expanding or 'summary' not in covered:
            weights['summarize'] = 1
    for flag, cap, task in (('classify', 'classified', 'classify'), ('assess', 'assessed', 'assess'),
                             ('motivation', 'motivation', 'motivation')):
        if getattr(profile, flag) and (cap not in covered or flag == 'motivation' and expanding):
            weights[task] = 1
    if profile.translate and ('translated_' + profile.extract not in covered):
        existing = max((scope_fraction[s] for s in SCOPES[1:] if 'translated_' + s in covered), default=0)
        weights['translate'] = max(0, scope_fraction[profile.extract] - existing)
        if not existing:
            weights['glossary'] = 1
        weights['term_review'] = scope_fraction[profile.extract]
    return {'tokens': round(sum(tasks[k]['tokens'] * w for k, w in weights.items())),
            'usd': round(sum(tasks[k]['usd'] * w for k, w in weights.items()), 4),
            'estimated': True, 'tasks': weights}
