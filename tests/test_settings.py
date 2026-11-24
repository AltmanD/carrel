import json

import pytest
from fastapi.testclient import TestClient

from carrel.config import MODEL_TASKS, ModelConfig, load_config
from carrel.model.verification import fingerprint, report_path, verification_status, task_cases, load_sample, _check_reply
from carrel.providers import load_provider_registry
from carrel.store.paths import atomic_write_json, paper_dir
from carrel.web.app import create_app
from .test_providers import provider

HEADERS = {"X-Carrel": "1"}


@pytest.fixture
def settings(config, db):
    return TestClient(create_app(config, db)), config


def add(client, **kwargs):
    return client.post("/api/providers", json=provider(**kwargs).to_dict(), headers=HEADERS)


def test_page_and_empty_config(settings):
    c, cfg = settings
    response = c.get("/settings")
    assert response.status_code == 200
    assert "任务分配" in response.text and "settings.js" in response.text
    assert c.get("/api/providers").json()["providers"] == []
    assert c.app.state.jobs.backend is c.app.state.router
    assert c.app.state.router.name.startswith("legacy:")
    assert "providers" in c.get("/api/health").json()


@pytest.mark.parametrize("headers,allowed", [({}, False), ({"Sec-Fetch-Site": "cross-site", "X-Carrel": "1"}, False),
                                            ({"Sec-Fetch-Site": "same-site"}, False), ({"Sec-Fetch-Site": "same-origin"}, True),
                                            ({"Sec-Fetch-Site": "none"}, True), (HEADERS, True)])
def test_csrf_all_writes(settings, headers, allowed):
    c, _ = settings
    for method, path, body in (("POST", "/api/providers", provider().to_dict()),
                                ("POST", "/api/task-routes", {"routes": {}}),
                                ("POST", "/api/providers/missing/test", {}),
                                ("POST", "/api/providers/missing/verify", {}),
                                ("DELETE", "/api/providers/missing", None)):
        response = c.request(method, path, headers=headers, json=body)
        assert (response.status_code != 403) == allowed


def test_edit_redaction_persistence_and_hot_reload(settings, db):
    c, cfg = settings
    router = c.app.state.router
    assert add(c, extra_headers={"Authorization": "Bearer hidden-secret"}).status_code == 200
    listing = c.get("/api/providers")
    assert provider().api_key not in listing.text and "hidden-secret" not in listing.text
    assert c.post("/api/task-routes", headers=HEADERS, json={"routes": {"translate": {"provider": "relay", "name": "different-model"}}}).status_code == 200
    assert c.app.state.router is router
    assert router.name_for("translate") == "relay:different-model"
    assert c.post("/api/providers", headers=HEADERS, json={"id": "relay", "api_key": "   ", "label": "renamed"}).status_code == 200
    registry = load_provider_registry(cfg.data_dir / "providers.toml")
    assert registry.providers["relay"].api_key == provider().api_key
    public = c.get("/api/providers").json()["providers"][0]
    public.pop("configured")
    assert c.post("/api/providers", headers=HEADERS, json=public).status_code == 200
    assert load_provider_registry(cfg.data_dir / "providers.toml").providers["relay"].extra_headers["Authorization"] == "Bearer hidden-secret"
    cfg2 = load_config(data_dir=cfg.data_dir)
    c2 = TestClient(create_app(cfg2, db))
    assert c2.app.state.router.name_for("translate") == "relay:different-model"
    assert c2.get("/api/providers").json()["providers"][0]["label"] == "renamed"


def test_delete_references_and_invalid_routes(settings):
    c, _ = settings
    add(c)
    for routes in ({"default_provider": "relay", "routes": {}}, {"routes": {"extract": {"provider": "relay"}}}):
        assert c.post("/api/task-routes", headers=HEADERS, json=routes).status_code == 200
        assert c.delete("/api/providers/relay", headers=HEADERS).status_code == 409
    for body in ({"routes": {"bad-task": {}}}, {"routes": {"translate": {"provider": "absent"}}}, {"routes": {"translate": {"max_tokens": -1}}}):
        assert c.post("/api/task-routes", headers=HEADERS, json=body).status_code == 422
    c.post("/api/task-routes", headers=HEADERS, json={"default_provider": "", "routes": {}})
    assert c.delete("/api/providers/relay", headers=HEADERS).status_code == 200


def test_recommended_gate_requires_all_tasks_and_current_config(settings):
    c, cfg = settings
    p = provider()
    add(c)
    apply = lambda: c.post("/api/task-routes", headers=HEADERS, json={"recommend": "relay"})
    assert apply().status_code == 409
    report = {"fingerprint": fingerprint(p), "passed": True, "baseline_passed": True,
              "tasks": [{"task": t, "passed": True} for t in MODEL_TASKS]}
    report["tasks"].pop()
    atomic_write_json(report_path(cfg.data_dir, p.id), report)
    assert apply().status_code == 409
    report["tasks"] = [{"task": t, "passed": True} for t in MODEL_TASKS]
    atomic_write_json(report_path(cfg.data_dir, p.id), report)
    assert apply().status_code == 200
    assert len(c.get("/api/task-routes").json()["routes"]) == len(MODEL_TASKS)
    c.post("/api/providers", headers=HEADERS, json={"id": "relay", "model": "changed"})
    assert apply().status_code == 409


def test_connection_test_does_not_unlock_recommendation(settings, monkeypatch):
    c, _ = settings
    add(c)
    monkeypatch.setattr("carrel.web.app.probe_provider", lambda *a: {"ok": True, "model": "gpt-test", "latency_ms": 1})
    assert c.post("/api/providers/relay/test", headers=HEADERS).json()["ok"]
    assert not c.get("/api/providers/relay/verification").json()["passed"]


def test_failed_save_leaves_running_config_intact(settings, monkeypatch):
    c, cfg = settings
    add(c)
    original = (cfg.data_dir / "providers.toml").read_text(encoding="utf-8")
    def fail(*args):
        raise OSError("read only")
    monkeypatch.setattr("carrel.providers.ProviderRegistry.save", fail)
    response = c.post("/api/providers", headers=HEADERS, json={"id": "relay", "model": "must-not-apply"})
    assert response.status_code == 503
    assert (cfg.data_dir / "providers.toml").read_text(encoding="utf-8") == original
    assert c.app.state.router.providers["relay"].model == "gpt-test"


def test_all_verification_cases_use_real_sample(config, pipeline_factory, tmp_path, db):
    pdf = tmp_path / "seed.pdf"
    pdf.write_bytes(b"%PDF-1.4 seed")
    pid = pipeline_factory().process(str(pdf))
    sample = load_sample(config.data_dir, pid)
    assert sample["blocks"] and "Existing orchestration" in sample["blocks"][0]["text"]
    assert sample["paper"] == pid
    assert sample["title"] == db.get_paper(pid)["title"]
    automatic = load_sample(config.data_dir)
    assert (automatic["paper"], automatic["title"]) == (pid, sample["title"])
    other_id = "arxiv:2401.01234"
    db.upsert_paper({"id": other_id, "title": "Earlier orchestration work", "year": 2024})
    atomic_write_json(paper_dir(config.data_dir, other_id) / "motivation.json", {"motivation": "Improve orchestration"})
    assert load_sample(config.data_dir, pid)["neighbors"] == [
        {"id": other_id, "title": "Earlier orchestration work", "year": 2024, "motivation": "Improve orchestration"}
    ]
    cases = task_cases(sample)
    assert set(cases) == set(MODEL_TASKS)
    assert all(schema["type"] == "object" and system and user for schema, system, user in cases.values())


def test_verification_rejects_flattened_and_bad_translation():
    from carrel.model.base import ModelReply
    from carrel.translate.translator import TRANSLATE_SCHEMA
    blocks = [{"id": "b1", "text": "We improve by 12% [3]."}]
    reply = ModelReply(text='{"translations": [{"block_id": "b1", "text": "提高 13%。"}]}',
                       tokens_in=10, tokens_out=10, stop_reason="completed")
    errors, coverage, hard = _check_reply("translate", reply, TRANSLATE_SCHEMA, blocks)
    assert errors and hard and coverage == 1
    reply.text = '[{"block_id":"b1","text":"提高 12% [3]。"}]'
    errors, _, _ = _check_reply("translate", reply, TRANSLATE_SCHEMA, blocks)
    assert errors


def test_health_returns_nonzero_on_probe_failure(config, db, monkeypatch, capsys):
    from argparse import Namespace
    from carrel.cli import cmd_health
    from carrel.model.base import Capabilities
    monkeypatch.setattr("carrel.cli._build", lambda args: (config, db))
    def fail(self, **kw):
        self.capability_error = "authentication failed"
        return Capabilities()
    monkeypatch.setattr("carrel.model.base._HttpMixin.capabilities", fail)
    assert cmd_health(Namespace()) == 2
    assert "probe failed" in capsys.readouterr().err


def test_full_verification_report_and_failed_rerun(config, pipeline_factory, tmp_path, monkeypatch):
    from carrel.model.base import ModelReply
    from carrel.model.verification import verify_provider
    pdf = tmp_path / "seed.pdf"
    pdf.write_bytes(b"%PDF-1.4 seed")
    pid = pipeline_factory().process(str(pdf))
    blocks = load_sample(config.data_dir, pid)["blocks"]
    calls = []
    broken = {"value": False}
    class SampleBackend:
        def __init__(self, *args, **kwargs):
            self.model = "gpt-test" if args else "glm-test"
        def raw_structured(self, *, task, schema, **kwargs):
            calls.append((self.model, task))
            def value(s):
                kind = s.get("type")
                if kind == "object":
                    return {k: value(v) for k, v in s.get("properties", {}).items()}
                if kind == "array":
                    return []
                if kind == "string":
                    return (s.get("enum") or ["测试结果"])[0]
                if kind == "boolean":
                    return False
                return 1
            data = value(schema)
            if task in ("translate", "repair"):
                data = {"translations": [{"block_id": b["id"], "text": "中文译文 " + b["text"]} for b in blocks]}
            if broken["value"] and self.model == "gpt-test" and task == "translate":
                data = []
            return ModelReply(text=json.dumps(data), data=data, tokens_in=100, tokens_out=100,
                              model=self.model, requested_model=self.model, stop_reason="completed")
    config.model = ModelConfig(name="glm-test", resolved_api_key="baseline-key")
    p = provider()
    report = verify_provider(config, p, paper_id=pid, router_factory=SampleBackend)
    assert report["passed"] and verification_status(config.data_dir, p)["passed"]
    assert len(calls) == 2 * len(MODEL_TASKS) and {t for _, t in calls} == set(MODEL_TASKS)
    broken["value"] = True
    report = verify_provider(config, p, paper_id=pid, router_factory=SampleBackend)
    assert not report["passed"] and not verification_status(config.data_dir, p)["passed"]
