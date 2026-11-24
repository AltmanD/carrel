"""Read-only legacy classification projection and reviewable migration plans."""
import base64
import hashlib
import json
import sqlite3
import tomllib
import os
import uuid
from contextlib import closing
from pathlib import Path
from carrel.classify.taxonomy import project_path, Taxonomy
from carrel.store.paths import acquire_lock, release_lock, atomic_write_text, safe_dirname


def digest(data):
    return hashlib.sha256(data).hexdigest()


def project_classification(raw, renames=None):
    result = dict(raw)
    categories = []
    for path in raw.get('categories') or [raw]:
        value = project_path(path, renames)
        if value not in categories:
            categories.append(value)
    primary = next((p for p in categories if p['category_id'] == raw.get('primary_category_id')), categories[0])
    result.update(categories=categories, primary_category_id=primary['category_id'],
                  discipline=primary['discipline'], field=primary['field'], taxonomy_version=2)
    result.pop('subfield', None)
    return result


def plan_migration(data_dir: Path, renames=None, keyword_policy=None):
    if renames is None:
        overlay = data_dir / 'taxonomy.toml'
        renames = tomllib.loads(overlay.read_text(encoding='utf-8')).get('rename', []) if overlay.exists() else []
    paper_rows = {}
    db_path = data_dir / 'app.db'
    if db_path.exists():
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            paper_rows = {safe_dirname(r['id']): dict(r) for r in conn.execute('SELECT id, discipline, field FROM papers')}
    entries = []
    db_tags = {}
    if db_path.exists():
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            db_tags = {pid: [dict(tag) for tag in conn.execute(
                'SELECT tag,evidence,source_block,origin FROM tags WHERE paper_id=? ORDER BY tag',
                (row['id'],))] for row in paper_rows.values() for pid in [row['id']]}
    for path in sorted((data_dir / 'papers').glob('*/classification.json')):
        before = path.read_bytes()
        raw = json.loads(before)
        projected = project_classification(raw, renames)
        # Only explicitly reviewed legacy terms become active keywords. Existing
        # tag objects, including unknown provenance fields, are never rewritten.
        for old in raw.get("categories") or [raw]:
            term = old.get("subfield")
            if term and (keyword_policy or {}).get(term) == "promote":
                tags = list(projected.get("tags") or [])
                names = {t.get("tag", t.get("name")) if isinstance(t, dict) else t for t in tags}
                if term not in names:
                    tags.append({"tag": term, "origin": old.get("origin", raw.get("origin", "legacy")),
                                 "evidence": "Reviewed legacy taxonomy keyword", "legacy_path": old})
                projected["tags"] = tags
        after = (json.dumps(projected, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        row = paper_rows.get(path.parent.name)
        paper_id = row['id'] if row else path.parent.name
        tags_before = db_tags.get(paper_id, [])
        tags_after = list(tags_before)
        for old in raw.get('categories') or [raw]:
            term = old.get('subfield')
            if term and (keyword_policy or {}).get(term) == 'promote' and not any(t['tag'].casefold() == term.casefold() for t in tags_after):
                tags_after.append({'tag': term, 'evidence': 'Reviewed legacy taxonomy keyword',
                                   'source_block': '', 'origin': old.get('origin', raw.get('origin', 'legacy'))})
        entries.append({'paper_id': paper_id, 'database_before': row,
                        'tags_before': tags_before, 'tags_after': sorted(tags_after, key=lambda t: t['tag']),
                        'path': path.relative_to(data_dir).as_posix(),
                        'input_hash': digest(before), 'output_hash': digest(after),
                        'before': base64.b64encode(before).decode(), 'after': base64.b64encode(after).decode(),
                        'old': raw, 'new': projected, 'source': raw.get('origin', 'legacy'), 'state': 'pending'})
    overlay = data_dir / "taxonomy.toml"
    taxonomy = Taxonomy.load(overlay, fallback=False)
    for entry in entries:
        review = []
        for old in entry["old"].get("categories") or [entry["old"]]:
            sub = old.get("subfield")
            if sub:
                mapped = project_path(old, renames)
                changed = (mapped["discipline"], mapped["field"]) != (old.get("discipline", ""), old.get("field", ""))
                policy = (keyword_policy or {}).get(sub, "review")
                review.append({"legacy_value": sub, "source": old.get("origin", entry["source"]),
                               "decision": "mapped" if changed else "ambiguous",
                               "keyword_policy": policy,
                               "exclusion_reason": "" if policy == "promote" else
                                   "Explicit exclusion" if policy == "exclude" else "No reviewed keyword promotion policy"})
        entry["review"] = review
        for category in entry["new"]["categories"]:
            taxonomy.add_path(category)
    overlay_before = overlay.read_bytes() if overlay.exists() else None
    overlay_after = taxonomy.to_toml().encode("utf-8")
    categories_before = None
    tags_before = None
    if db_path.exists():
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='tags'").fetchone():
                tags_before = [list(row) for row in conn.execute("SELECT paper_id,tag,evidence,source_block,origin FROM tags ORDER BY paper_id,tag")]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='categories'").fetchone():
                categories_before = [list(row) for row in conn.execute("SELECT path,label,parent,sort FROM categories ORDER BY path")]
    tags_after = list(tags_before or [])
    for entry in entries:
        tags_after = [row for row in tags_after if row[0] != entry["paper_id"]]
        tags_after.extend([[entry["paper_id"], tag["tag"], tag.get("evidence", ""),
                            tag.get("source_block", ""), tag.get("origin", "legacy")]
                           for tag in entry["tags_after"]])
    tags_after.sort(key=lambda row: (row[0], row[1]))
    return {"version": 2, "root": str(data_dir.resolve()), "renames": renames or [], "entries": entries,
            "tags_before": tags_before, "tags_after": tags_after,
            "overlay_before": base64.b64encode(overlay_before).decode() if overlay_before is not None else None,
            "overlay_after": base64.b64encode(overlay_after).decode(),
            "categories_before": categories_before, "categories_after": sorted(
                [[r[k] for k in ("path", "label", "parent", "sort")] for r in taxonomy.category_rows()])}


def execute_plan(data_dir: Path, manifest: Path, *, rollback=False):
    data_dir = data_dir.resolve()
    plan = json.loads(manifest.read_text(encoding='utf-8'))
    if plan['root'] != str(data_dir):
        raise ValueError('Migration plan belongs to a different library')
    lock = acquire_lock(data_dir)
    if lock is None:
        raise RuntimeError('Stop the library service before migrating')
    try:
        db_path = data_dir / 'app.db'
        conn = sqlite3.connect(db_path) if db_path.exists() else None
        if conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
            if not plan.get("backup"):
                backup = manifest.with_suffix(manifest.suffix + ".sqlite3")
                if backup.exists():
                    raise ValueError("Backup already exists without manifest ownership")
                plan["backup"] = str(backup.resolve())
                plan["backup_temp"] = str(backup.resolve()) + "." + uuid.uuid4().hex + ".tmp"
                plan["backup_state"] = "pending"
                atomic_write_text(manifest, json.dumps(plan, ensure_ascii=False, indent=2))
            if plan.get("backup_state") == "pending":
                # Ownership is durable before backup creation; no library writes
                # occur until the completed backup is checkpointed as ready.
                temporary = Path(plan["backup_temp"])
                temporary.unlink(missing_ok=True)
                with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(temporary)) as destination:
                        source.backup(destination)
                with temporary.open("r+b") as backup_file:
                    os.fsync(backup_file.fileno())
                os.replace(temporary, plan["backup"])
                plan["backup_state"] = "ready"
                atomic_write_text(manifest, json.dumps(plan, ensure_ascii=False, indent=2))
            if plan.get("categories_before") is not None:
                rows = [list(row) for row in conn.execute("SELECT path,label,parent,sort FROM categories ORDER BY path")]
                if rows not in (plan["categories_before"], plan["categories_after"]):
                    raise ValueError("Categories migration conflict")
            if plan.get("tags_before") is not None:
                rows = [list(row) for row in conn.execute("SELECT paper_id,tag,evidence,source_block,origin FROM tags ORDER BY paper_id,tag")]
                if rows not in (plan["tags_before"], plan["tags_after"]):
                    raise ValueError("Keywords migration conflict")
            for entry in plan['entries']:
                before = entry.get('database_before')
                if before:
                    row = conn.execute('SELECT discipline, field FROM papers WHERE id=?', (entry['paper_id'],)).fetchone()
                    allowed = [(before['discipline'], before['field']), (entry['new']['discipline'], entry['new']['field'])]
                    if row is None or tuple(row) not in allowed:
                        raise ValueError('Database migration conflict: ' + entry['paper_id'])
                tag_rows = [dict(r) for r in conn.execute(
                    'SELECT tag,evidence,source_block,origin FROM tags WHERE paper_id=? ORDER BY tag',
                    (entry['paper_id'],))]
                if tag_rows not in (entry.get('tags_before', []), entry.get('tags_after', [])):
                    raise ValueError('Tag migration conflict: ' + entry['paper_id'])
        overlay = data_dir / "taxonomy.toml"
        overlay_current = base64.b64encode(overlay.read_bytes()).decode() if overlay.exists() else None
        if "overlay_after" in plan and overlay_current not in (plan["overlay_before"], plan["overlay_after"]):
            raise ValueError("Taxonomy overlay migration conflict")
        for entry in plan['entries']:
            path = (data_dir / entry['path']).resolve()
            if not path.is_relative_to(data_dir / 'papers'):
                raise ValueError('Unsafe migration path')
            if digest(path.read_bytes()) not in (entry['input_hash'], entry['output_hash']):
                raise ValueError('Migration conflict: ' + entry['path'])
        for entry in plan['entries']:
            path = data_dir / entry['path']
            target = base64.b64decode(entry['before' if rollback else 'after'])
            if digest(path.read_bytes()) != digest(target):
                atomic_write_text(path, target.decode('utf-8'))
            entry['state'] = 'rolled_back' if rollback else 'applied'
            atomic_write_text(manifest, json.dumps(plan, ensure_ascii=False, indent=2))
            if conn and entry.get('database_before'):
                value = entry['database_before'] if rollback else entry['new']
                conn.execute('UPDATE papers SET discipline=?, field=? WHERE id=?',
                             (value['discipline'], value['field'], entry['paper_id']))
            if conn:
                target_rows = entry.get('tags_before' if rollback else 'tags_after', [])
                conn.execute('DELETE FROM tags WHERE paper_id=?', (entry['paper_id'],))
                conn.executemany('INSERT INTO tags(paper_id,tag,evidence,source_block,origin) VALUES(?,?,?,?,?)',
                                 [(entry['paper_id'], row['tag'], row.get('evidence', ''),
                                   row.get('source_block', ''), row.get('origin', 'legacy'))
                                  for row in target_rows])
        if "overlay_after" in plan:
            target = plan["overlay_before" if rollback else "overlay_after"]
            if target is None:
                overlay.unlink(missing_ok=True)
            else:
                atomic_write_text(overlay, base64.b64decode(target).decode("utf-8"))
        if conn and plan.get("categories_before") is not None:
            conn.execute("DELETE FROM categories")
            conn.executemany("INSERT INTO categories(path,label,parent,sort) VALUES(?,?,?,?)",
                             plan["categories_before" if rollback else "categories_after"])
        if conn and plan.get("tags_before") is not None:
            conn.execute("DELETE FROM tags")
            conn.executemany("INSERT INTO tags(paper_id,tag,evidence,source_block,origin) VALUES(?,?,?,?,?)",
                             plan["tags_before" if rollback else "tags_after"])
        if conn:
            conn.commit()
        return plan
    finally:
        if 'conn' in locals() and conn:
            conn.close()
        release_lock(lock)
