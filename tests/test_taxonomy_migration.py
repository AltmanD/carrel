import json
import sqlite3
import tomllib

import pytest

from carrel.classify.taxonomy import CATEGORY_KEYS, Taxonomy, project_path
from carrel.classify.migration import plan_migration, execute_plan


def library(tmp_path):
    paper = tmp_path / "papers" / "p" / "classification.json"
    paper.parent.mkdir(parents=True)
    paper.write_text(json.dumps({"discipline": "Science", "field": "Methods",
                                "subfield": "Unknown", "tags": [{"tag": "mine", "origin": "manual"}]}))
    with sqlite3.connect(tmp_path / "app.db") as conn:
        conn.executescript("CREATE TABLE papers(id PRIMARY KEY, discipline, field);"
                           "INSERT INTO papers VALUES('p','Science','Methods');"
                           "CREATE TABLE categories(path PRIMARY KEY,label,parent,sort);"
                           "CREATE TABLE tags(paper_id,tag,evidence,source_block,origin, PRIMARY KEY(paper_id,tag));"
                           "INSERT INTO tags VALUES('p','mine','manual evidence','','human');"
                           "CREATE TABLE unrelated(value); INSERT INTO unrelated VALUES(42);")
    manifest = tmp_path / "migration.json"
    manifest.write_text(json.dumps(plan_migration(tmp_path)), encoding="utf-8")
    return paper, manifest


def test_two_levels_and_identity_roundtrip():
    assert CATEGORY_KEYS == ("discipline", "field")
    taxonomy = Taxonomy(disciplines=["A"], fields={"A": ["B"]})
    identity = taxonomy.id_for("A", "B")
    renamed = taxonomy.renamed({"discipline": "A", "field": "B"}, "C")
    renamed = Taxonomy.from_raw(tomllib.loads(renamed.to_toml()))
    assert renamed.id_for("A", "C") == identity
    assert all("subfield" not in path for path in renamed.paths())
    assert project_path({"discipline": "A", "field": "B", "subfield": "unknown"})["field"] == "B"


def test_apply_resume_rollback_preserves_data(tmp_path):
    paper, manifest = library(tmp_path)
    before = paper.read_bytes()
    plan = execute_plan(tmp_path, manifest)
    assert plan["entries"][0]["review"][0]["decision"] == "ambiguous"
    assert json.loads(paper.read_text())["tags"] == [{"tag": "mine", "origin": "manual"}]
    after = paper.read_bytes()
    execute_plan(tmp_path, manifest)
    assert paper.read_bytes() == after
    with sqlite3.connect(plan["backup"]) as conn:
        assert conn.execute("SELECT value FROM unrelated").fetchone() == (42,)
    execute_plan(tmp_path, manifest, rollback=True)
    execute_plan(tmp_path, manifest, rollback=True)
    assert paper.read_bytes() == before
    assert not (tmp_path / "taxonomy.toml").exists()
    with sqlite3.connect(tmp_path / "app.db") as conn:
        assert conn.execute("SELECT value FROM unrelated").fetchone() == (42,)


def test_conflict_and_interrupted_resume(tmp_path, monkeypatch):
    paper, manifest = library(tmp_path)
    import carrel.classify.migration as migration
    write = migration.atomic_write_text
    def interrupt(path, text):
        write(path, text)
        if path == paper:
            raise KeyboardInterrupt()
    monkeypatch.setattr(migration, "atomic_write_text", interrupt)
    with pytest.raises(KeyboardInterrupt):
        execute_plan(tmp_path, manifest)
    monkeypatch.setattr(migration, "atomic_write_text", write)
    execute_plan(tmp_path, manifest)
    paper.write_text("{}")
    with pytest.raises(ValueError, match="Migration conflict"):
        execute_plan(tmp_path, manifest, rollback=True)

@pytest.mark.parametrize("stage", ["backup", "checkpoint", "overlay"])
def test_interruptions_resume(tmp_path, monkeypatch, stage):
    import carrel.classify.migration as migration
    paper, manifest = library(tmp_path)
    original = paper.read_bytes()
    write = migration.atomic_write_text
    replace = migration.os.replace
    def interrupt_write(path, text):
        if stage == "checkpoint" and path == manifest and json.loads(text).get("backup_state") == "ready":
            raise KeyboardInterrupt()
        write(path, text)
        if stage == "overlay" and path == tmp_path / "taxonomy.toml":
            raise KeyboardInterrupt()
    def interrupt_replace(source, target):
        if stage == "backup" and str(target).endswith(".sqlite3"):
            raise KeyboardInterrupt()
        replace(source, target)
    monkeypatch.setattr(migration, "atomic_write_text", interrupt_write)
    monkeypatch.setattr(migration.os, "replace", interrupt_replace)
    with pytest.raises(KeyboardInterrupt):
        execute_plan(tmp_path, manifest)
    monkeypatch.setattr(migration, "atomic_write_text", write)
    monkeypatch.setattr(migration.os, "replace", replace)
    execute_plan(tmp_path, manifest)
    assert json.loads(paper.read_text())["taxonomy_version"] == 2
    execute_plan(tmp_path, manifest, rollback=True)
    assert paper.read_bytes() == original


def test_promotion_and_unrelated_edits_survive_rollback(tmp_path):
    paper, manifest = library(tmp_path)
    manifest.write_text(json.dumps(plan_migration(tmp_path, keyword_policy={"Unknown": "promote"})))
    execute_plan(tmp_path, manifest)
    assert any(tag["tag"] == "Unknown" for tag in json.loads(paper.read_text())["tags"])
    execute_plan(tmp_path, manifest)
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep this")
    with sqlite3.connect(tmp_path / "app.db") as conn:
        assert conn.execute("SELECT origin FROM tags WHERE tag='mine'").fetchone() == ("human",)
        assert conn.execute("SELECT origin FROM tags WHERE tag='Unknown'").fetchone() == ("legacy",)
        conn.execute("UPDATE unrelated SET value=99")
    execute_plan(tmp_path, manifest, rollback=True)
    execute_plan(tmp_path, manifest, rollback=True)
    assert unrelated.read_text() == "keep this"
    with sqlite3.connect(tmp_path / "app.db") as conn:
        assert conn.execute("SELECT value FROM unrelated").fetchone() == (99,)
        assert conn.execute("SELECT tag,origin FROM tags").fetchall() == [("mine", "human")]

@pytest.mark.parametrize("stage", ["copy", "commit"])
def test_database_interruptions(tmp_path, monkeypatch, stage):
    import carrel.classify.migration as migration
    paper, manifest = library(tmp_path)
    connect = sqlite3.connect
    class InterruptedConnection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            if stage == "copy":
                target.execute("CREATE TABLE partial(value)")
                target.commit()
                raise KeyboardInterrupt()
            return super().backup(target, **kwargs)
        def commit(self):
            super().commit()
            if stage == "commit":
                raise KeyboardInterrupt()
    def interrupted_connect(*args, **kwargs):
        kwargs["factory"] = InterruptedConnection
        return connect(*args, **kwargs)
    monkeypatch.setattr(migration.sqlite3, "connect", interrupted_connect)
    with pytest.raises(KeyboardInterrupt):
        execute_plan(tmp_path, manifest)
    monkeypatch.setattr(migration.sqlite3, "connect", connect)
    execute_plan(tmp_path, manifest)
    assert json.loads(paper.read_text())["taxonomy_version"] == 2
    execute_plan(tmp_path, manifest, rollback=True)
