"""Constrained taxonomy.

The model may only choose an existing path. When nothing fits it must return
"未分类" and the app records a proposal for a human to accept — that is the only
thing keeping the tree from fragmenting after a few dozen papers.
"""

from __future__ import annotations

import json
import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNCLASSIFIED = "未分类"
CATEGORY_KEYS = ("discipline", "field")
LEGACY_CATEGORY_KEYS = (*CATEGORY_KEYS, "subfield")

def category_id(discipline: str, field_name: str = "") -> str:
    return "cat-" + hashlib.sha256(json.dumps([discipline, field_name], ensure_ascii=False).encode()).hexdigest()[:20]

def project_path(path: dict, renames: list[dict] | None = None) -> dict:
    """Read-only legacy projection; apply historical renames before mapping."""
    value = dict(path)
    original_id = project_path(path)["category_id"] if renames else None
    reviewed_subfield = False
    for rename in renames or []:
        if matches_category(value, rename):
            key = next((k for k in reversed(LEGACY_CATEGORY_KEYS) if rename.get(k)), None)
            if key:
                value[key] = rename['name']
                reviewed_subfield = reviewed_subfield or key == "subfield"
    disc, fld, sub = (value.get(k) or "" for k in LEGACY_CATEGORY_KEYS)
    mapping = {
        ("机器学习", "强化学习"): ("模型训练与对齐", "强化学习"),
        ("机器学习", "训练方法"): ("模型训练与对齐", "训练方法"),
        ("机器学习", "模型评测"): ("评测与安全", "能力评测"),
        ("机器学习", "推理"): ("推理与系统", "推理方法"),
        ("机器学习", "表示学习"): ("模型训练与对齐", "表示学习"),
        ("自然语言处理", "对齐"): ("模型训练与对齐", "对齐"),
        ("自然语言处理", "智能体"): ("智能体", "通用智能体"),
        ("自然语言处理", "生成"): ("语言理解与生成", "文本生成"),
        ("自然语言处理", "理解"): ("语言理解与生成", "语言理解"),
        ("信息检索", "稠密检索"): ("检索与知识", "语义检索"),
        ("信息检索", "知识组织"): ("检索与知识", "知识组织"),
        ("安全", "提示注入"): ("评测与安全", "智能体安全"),
    }
    if disc == "计算机科学" and (fld, sub) in mapping:
        disc, fld = mapping[fld, sub]
    elif sub and reviewed_subfield:
        disc, fld = fld or disc, sub
    return {**{k: path[k] for k in ('origin', 'evidence', 'source_block') if k in path},
            "discipline": disc, "field": fld, "category_id": value.get("category_id") or original_id or category_id(disc, fld)}


def matches_category(path: dict, source: dict) -> bool:
    return all(not source.get(key) or path.get(key) == source[key]
               for key in LEGACY_CATEGORY_KEYS)


@dataclass(slots=True)
class Leaf:
    discipline: str
    field: str = ""
    subfield: str | None = None

    @property
    def path(self) -> str:
        return self.discipline

    def label(self) -> str:
        bits = [self.discipline, self.field, self.subfield or ""]
        return " / ".join(b for b in bits if b)


@dataclass(slots=True)
class Taxonomy:
    disciplines: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)
    subfields: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    renames: list[dict[str, str]] = field(default_factory=list)
    identities: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path, *, fallback: bool = True) -> "Taxonomy":
        if not path.is_file():
            return cls.default() if fallback else cls()
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        return cls.from_raw(raw, fallback=fallback)

    @classmethod
    def from_raw(cls, raw: dict, *, fallback: bool = True) -> "Taxonomy":
        disciplines: list[str] = []
        fields: dict[str, list[str]] = {}
        subfields: dict[str, dict[str, list[str]]] = {}
        for disc, body in (raw.get("discipline") or {}).items():
            disciplines.append(disc)
            fields[disc] = list(body.get("fields") or [])
            sub_map = {k: list(v) for k, v in (body.get("subfields") or {}).items()}
            if sub_map:
                subfields[disc] = sub_map
        if not disciplines and fallback and not raw.get("rename"):
            return cls.default()
        result = cls(disciplines=disciplines, fields=fields,
                     renames=list(raw.get("rename") or []), identities=dict(raw.get("identities") or {}))
        for disc, groups in subfields.items():
            for fld, terms in groups.items():
                for sub in terms:
                    result.add_path(project_path({"discipline": disc, "field": fld, "subfield": sub}, result.renames))
        return result

    @classmethod
    def layered(cls, packaged: Path, user_overlay: Path) -> "Taxonomy":
        """Packaged taxonomy overlaid with the user's additions.

        The overlay lives in the data directory, so manual categories survive
        package upgrades and travel with the library on backup.
        """
        base = cls.load(packaged)
        if user_overlay.is_file():
            try:
                with user_overlay.open("rb") as fh:
                    raw = tomllib.load(fh)
                user = cls.from_raw(raw, fallback=False)
                # Rename packaged paths before merging current user additions.
                for rename in user.renames:
                    base = base.renamed(rename, rename["name"])
                base = base.merged(user)
            except Exception:  # noqa: BLE001 - a broken overlay must not brick the app
                pass
        return base

    def merged(self, other: "Taxonomy") -> "Taxonomy":
        """Union of both trees; order keeps self first, new entries appended."""
        disciplines = list(self.disciplines)
        fields = {k: list(v) for k, v in self.fields.items()}
        subfields = {d: {f: list(s) for f, s in m.items()}
                     for d, m in self.subfields.items()}
        for disc in other.disciplines:
            if disc not in disciplines:
                disciplines.append(disc)
                fields[disc] = []
                subfields.setdefault(disc, {})
        for disc, flds in other.fields.items():
            if disc not in fields:
                continue
            for f in flds:
                if f not in fields[disc]:
                    fields[disc].append(f)
                    subfields.setdefault(disc, {}).setdefault(f, [])
        for disc, subs in other.subfields.items():
            for f, ss in subs.items():
                bucket = subfields.setdefault(disc, {}).setdefault(f, [])
                for s in ss:
                    if s not in bucket:
                        bucket.append(s)
        return Taxonomy(disciplines=disciplines, fields=fields,
                        renames=[*self.renames, *other.renames],
                        identities={**self.identities, **other.identities})

    def add_path(self, path: dict[str, str]) -> None:
        if path.get("subfield"):
            path = project_path(path, self.renames)
        disc, fld, sub = (path.get(key) or "" for key in LEGACY_CATEGORY_KEYS)
        if path.get("category_id"):
            self.identities[json.dumps([disc, fld], ensure_ascii=False)] = path["category_id"]
        if not disc:
            return
        if disc not in self.disciplines:
            self.disciplines.append(disc)
        fields = self.fields.setdefault(disc, [])
        if fld and fld not in fields:
            fields.append(fld)
        if sub:
            self.add_path(project_path(path, self.renames))

    def renamed(self, source: dict[str, str], name: str) -> "Taxonomy":
        key = next(key for key in reversed(LEGACY_CATEGORY_KEYS) if source.get(key))
        result = Taxonomy(renames=list(self.renames))
        for path in self.paths():
            path["category_id"] = self.id_for(path["discipline"], path["field"])
            if matches_category(path, source):
                path[key] = name
            result.add_path(path)
        return result

    def id_for(self, discipline: str, field_name: str = "") -> str:
        """Use the original path identity across recorded display renames."""
        registered = self.identities.get(json.dumps([discipline, field_name], ensure_ascii=False))
        if registered:
            return registered
        path = {"discipline": discipline, "field": field_name}
        for rename in reversed(self.renames):
            key = next((k for k in reversed(LEGACY_CATEGORY_KEYS) if rename.get(k)), None)
            if key and key != "subfield":
                target = {k: v for k, v in rename.items() if k in CATEGORY_KEYS}
                target[key] = rename['name']
                if matches_category(path, target):
                    path[key] = rename[key]
        return category_id(path['discipline'], path['field'])

    def to_toml(self) -> str:
        """Serialise as the overlay file format (only what this tree holds)."""
        lines = ["[identities]"]
        for path in self.paths():
            key = json.dumps([path["discipline"], path["field"]], ensure_ascii=False)
            lines.append(json.dumps(key, ensure_ascii=False) + " = " +
                         json.dumps(self.id_for(path["discipline"], path["field"])))
        lines.append("")
        for rename in self.renames:
            lines.append("[[rename]]")
            for key in (*LEGACY_CATEGORY_KEYS, "name"):
                if key == "subfield" and not rename.get(key):
                    continue
                lines.append(f"{key} = " + json.dumps(rename.get(key, ""), ensure_ascii=False))
            lines.append("")
        for disc in self.disciplines:
            flds = self.fields.get(disc, [])
            quoted_disc = json.dumps(disc, ensure_ascii=False)
            lines.append(f"[discipline.{quoted_disc}]")
            lines.append("fields = " + json.dumps(flds, ensure_ascii=False))
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def default(cls) -> "Taxonomy":
        return cls.load(Path(__file__).parents[1] / "taxonomy.toml", fallback=False)

    # ----------------------------------------------------------- queries
    def paths(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for disc in self.disciplines:
            out.append({"discipline": disc, "field": ""})
            for fld in self.fields.get(disc, []):
                out.append({"discipline": disc, "field": fld})
        return out

    def prompt_table(self) -> str:
        lines = [f"- {p['discipline']}" for p in self.paths() if not p["field"]]
        for disc in self.disciplines:
            for fld in self.fields.get(disc, []):
                lines.append(f"- {disc} / {fld}")
        return "\n".join(lines)

    def validate(self, discipline: str, field_name: str, subfield: str | None) -> tuple[bool, str]:
        if discipline not in self.disciplines:
            return False, f"未知领域：{discipline}"
        allowed_fields = self.fields.get(discipline, [])
        if field_name and field_name not in allowed_fields:
            return False, f"领域「{discipline}」下没有「{field_name}」"
        if subfield:
            return False, "分类仅支持两级"
        return True, ""

    def category_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i, disc in enumerate(self.disciplines):
            rows.append({"path": disc, "label": disc, "parent": "", "sort": i})
            for j, fld in enumerate(self.fields.get(disc, [])):
                rows.append({
                    "path": json.dumps([disc, fld], ensure_ascii=False),
                    "label": fld, "parent": disc, "sort": j,
                })
        return rows
