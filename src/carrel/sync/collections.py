"""Directory-to-collection rules, independent of the academic taxonomy."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path


def load_rules(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    rules = data.get("collections", [])
    if not isinstance(rules, list):
        raise ValueError("collections.toml: collections must be an array of tables")
    for rule in rules:
        if not isinstance(rule, dict) or not all(isinstance(rule.get(k), str) for k in ("path", "name")):
            raise ValueError("Each collection rule needs a path and name")
        if not rule["name"].strip() or rule.get("kind", "dir") not in ("dir", "batch", "capability"):
            raise ValueError("Invalid collection name or kind")
    return rules


def collection_for(root: Path, pdf: Path, rules: list[dict]) -> tuple[dict, int | None]:
    parts = pdf.relative_to(root).parts
    folder = root / parts[0] if len(parts) > 1 else root
    name, kind = "未归类", "dir"
    if len(parts) > 1:
        name = re.sub(r"^\d{8}[_-]|[-_]reading$|[-_]\d{8}$", "", parts[0])
        if parts[0].startswith("能力域") and len(parts) > 2:
            folder = root / parts[0] / parts[1]
            name, kind = "能力域 / " + re.sub(r"^\d{1,2}[_-]", "", parts[1]), "capability"
        elif (folder / "paper_index.json").is_file() or (len(parts) > 2 and re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", parts[1])):
            kind = "batch"
    matching = []
    for rule in rules:
        candidate = Path(rule["path"])
        candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if candidate == root and pdf.parent != root:
            continue
        if pdf.is_relative_to(candidate):
            matching.append((len(candidate.parts), candidate, rule))
    if matching:
        _, folder, rule = max(matching, key=lambda item: item[0])
        name, kind = rule["name"].strip(), rule.get("kind", kind)
    digest = hashlib.sha256(str(folder.resolve()).casefold().encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-")[:48] or "collection"
    ranks = [re.match(r"^(\d{1,2})[_-]", p) for p in pdf.relative_to(folder).parts]
    rank = next((int(m[1]) for m in ranks if m), None)
    return {"id": f"{slug}-{digest}", "name": name, "kind": kind,
            "source_path": str(folder.resolve()), "note": "", "sort": 0}, rank


def rules_text(collections: list[dict]) -> str:
    lines = ["# Local reading collections. Paths may be absolute or relative to the scan root.",
             "# The most specific matching path wins. Existing library names are preserved.", ""]
    for c in collections:
        lines += ["[[collections]]", f"path = {json.dumps(c['source_path'], ensure_ascii=False)}",
                  f"name = {json.dumps(c['name'], ensure_ascii=False)}", f"kind = {json.dumps(c['kind'])}", ""]
    return "\n".join(lines)
