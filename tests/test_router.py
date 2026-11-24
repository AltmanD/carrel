import threading

import pytest

from carrel.config import ModelConfig, TaskModelConfig
from carrel.model.base import Capabilities, ModelError, ModelReply, ModelSchemaError
from carrel.model.router import ProviderRouter
from carrel.providers import RouteConfig
from carrel.store.runs import record_run
from .test_providers import provider


class EchoBackend:
    def __init__(self, cfg, **kwargs):
        self.cfg = cfg
        self.model = cfg.name
        self.by_task = {t: cfg.for_task(t) for t in ("translate", "motivation", "extract")}
        self.calls = []

    def text(self, **kw):
        self.calls.append(kw)
        name, thinking, limit = self.cfg.for_task(kw.get("task", ""))
        return ModelReply(text='{"ok":true}', data={"ok": True}, requested_model=name, model=name,
                          tokens_in=10, tokens_out=5, thinking=thinking, max_tokens=limit)

    raw_structured = structured = text

    def capabilities(self, **kw):
        self.calls.append(kw)
        return Capabilities(strict_schema=True)


def router(**kwargs):
    p, q = provider(), provider(id="second", model="second-model")
    return ProviderRouter({p.id: p, q.id: q}, {"translate": RouteConfig(p.id, "translation-model", "high", 321)},
                          fallback_config=ModelConfig(name="legacy-model"), backend_factory=EchoBackend, **kwargs)


def test_task_routing_and_legacy_fallback():
    r = router()
    reply = r.raw_structured(task="translate")
    assert (reply.provider, reply.model, reply.thinking, reply.max_tokens) == ("relay", "translation-model", "high", 321)
    assert not reply.substituted
    assert r.text(task="motivation").provider == "legacy"
    assert r.name == "legacy:legacy-model"
    assert r.name_for("translate") == "relay:translation-model"
    r.capabilities(task="translate")
    assert r.backend_for_provider("relay").calls[-1] == {"model": "translation-model", "thinking": "high"}


def test_title_inherits_preview_route_until_explicitly_overridden():
    r = router()
    r.update(r.providers, {'preview': RouteConfig('relay', 'preview-model', 'low', 1500)})
    reply = r.text(task='title')
    assert (reply.provider, reply.model, reply.thinking, reply.max_tokens) == ('relay', 'preview-model', 'low', 1500)
    r.update(r.providers, {'preview': RouteConfig('relay'), 'title': RouteConfig('second', 'title-model')})
    reply = r.text(task='title')
    assert (reply.provider, reply.model) == ('second', 'title-model')


def test_title_inherits_legacy_preview_override():
    cfg = ModelConfig(name='default', by_task={'preview': TaskModelConfig('preview-model', 'low', 1500)})
    assert cfg.for_task('title') == ('preview-model', 'low', 1500)


def test_default_inheritance_and_hot_update():
    r = router(default_provider="second")
    assert r.text(task="extract").provider == "second"
    r.update(r.providers, {"translate": RouteConfig(name="override-default", thinking="medium")}, default_provider="second")
    reply = r.text(task="translate")
    assert reply.provider == "second" and reply.model == "override-default" and reply.thinking == "medium"
    assert r.name_for("translate") == "second:override-default"


def test_legacy_task_fields_are_inherited():
    config = ModelConfig(name="legacy", by_task={"translate": TaskModelConfig("legacy-task", "high", 9000)})
    r = ProviderRouter(routes={"translate": RouteConfig(thinking="low")}, fallback_config=config, backend_factory=EchoBackend)
    reply = r.text(task="translate")
    assert (reply.model, reply.thinking, reply.max_tokens) == ("legacy-task", "low", 9000)


def test_keys_are_isolated_from_legacy_environment(monkeypatch):
    monkeypatch.setenv("CARREL_MODEL_API_KEY", "unrelated-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-other-key")
    r = router()
    assert r.backend_for_provider("relay").cfg.api_key == provider().api_key


def test_missing_key_fails_only_assigned_tasks():
    r = router()
    r.update({"relay": provider(api_key="")}, {"translate": RouteConfig("relay")})
    with pytest.raises(ModelError, match="/settings") as error:
        r.text(task="translate")
    assert error.value.reply.provider == "relay"
    assert r.text(task="motivation").provider == "legacy"


def test_in_flight_reply_keeps_provider_and_accounting(config, db):
    r = router()
    backend = r.backend_for_provider("relay")
    entered, release = threading.Event(), threading.Event()
    original = backend.text
    def delayed(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)
    backend.text = delayed
    replies = []
    thread = threading.Thread(target=lambda: replies.append(r.text(task="translate")))
    thread.start()
    assert entered.wait(3)
    r.update(r.providers, {"translate": RouteConfig("second")})
    release.set(); thread.join(3)
    reply = replies[0]
    assert reply.provider == "relay" and r.text(task="translate").provider == "second"
    rid = record_run(config, db, r, paper_id="p", task="translate", meta=reply.accounting())
    row = db.get_run(rid)
    assert row["provider"] == "relay" and row["backend"] == "relay:translation-model"
    assert db.usage_summary()["by_provider"][0]["provider"] == "relay"


@pytest.mark.parametrize("fail_retry", [False, True])
def test_structured_fallback_folds_spend(fail_retry):
    r = router()
    backend = r.backend_for_provider("relay")
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        reply = ModelReply(text='{}', tokens_in=10, tokens_out=5, cost_usd=1, model="translation-model")
        if len(calls) == 1 or fail_retry:
            raise ModelSchemaError("bad shape", reply)
        return reply
    backend.structured = call
    if fail_retry:
        with pytest.raises(ModelSchemaError) as error:
            r.structured_with_fallback(task="translate", schema={}, system="s")
        reply = error.value.reply
    else:
        reply = r.structured_with_fallback(task="translate", schema={}, system="s")
    assert (reply.tokens_in, reply.tokens_out, reply.cost_usd, reply.provider) == (20, 10, 2, "relay")


def test_json_retry_stays_on_original_provider():
    from carrel.model.base import call_json
    r = router()
    backend = r.backend_for_provider("relay")
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        reply = ModelReply(text='{}', data={}, model="translation-model", tokens_in=5, tokens_out=5)
        if len(calls) == 1:
            r.update(r.providers, {"translate": RouteConfig("second")})
            raise ModelSchemaError("bad JSON", reply)
        return reply
    backend.raw_structured = call
    reply = call_json(r, task="translate", schema={}, system="s", user="u")
    assert reply.provider == "relay" and reply.tokens_out == 10
    assert len(calls) == 2 and r.provider_for("translate") == "second"


def test_v3_migration_preserves_old_runs(tmp_path):
    from carrel.store.db import Database
    path = tmp_path / "old.db"
    db = Database(path)
    db.record_run({"id": "old", "paper_id": "p", "task": "translate", "tokens_out": 73})
    db.execute("ALTER TABLE model_runs DROP COLUMN provider")
    db.execute("UPDATE schema_meta SET value='3' WHERE key='version'")
    db.close()
    migrated = Database(path)
    try:
        row = migrated.get_run("old")
        assert row["provider"] == "legacy" and row["tokens_out"] == 73
        assert migrated.usage_summary()["by_provider"][0]["provider"] == "legacy"
    finally:
        migrated.close()
