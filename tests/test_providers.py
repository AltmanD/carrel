import json

import pytest

from carrel.config import load_config
from carrel.providers import ProviderConfig, ProviderRegistry, RouteConfig, load_provider_registry


def provider(**kwargs):
    return ProviderConfig(**{"id": "relay", "label": "中转 · GPT", "protocol": "responses",
                             "base_url": "https://relay.test/v1", "api_key": "sk-secret-12345678",
                             "model": "gpt-test", **kwargs})


def test_registry_roundtrip(tmp_path):
    p = provider(notes='中文\nTab\t引号"反斜线\\控制\u0001\x7f', extra_headers={"x-version": "v1"},
                 pricing={"input": 1.1, "output": 2.2, "per_model": {"gpt-test": {"input": 0.1}}}, max_retries=0)
    registry = ProviderRegistry({p.id: p}, {"translate": RouteConfig(p.id, "gpt-other", "low", 12000)}, p.id, tmp_path / "providers.toml")
    registry.save()
    saved = load_provider_registry(registry.path)
    assert saved.providers == registry.providers
    assert saved.routes == registry.routes
    assert saved.default_provider == "relay"
    assert not saved.error


def test_template_and_corrupt_file_fallback(tmp_path):
    path = tmp_path / "providers.toml"
    assert not load_provider_registry(path).providers
    assert path.exists() and "sk-" not in path.read_text()
    for malformed in ('[[provider', 'provider = 4', '[[provider]]\nid="relay"\ntimeout_s="bad"', '[task]\ntranslate=3'):
        path.write_text(malformed)
        result = load_provider_registry(path)
        assert not result.providers and not result.routes
        assert path.read_text() == malformed


def test_keys_and_env_priority(monkeypatch):
    p = provider(api_key_env="CARREL_TEST_PROVIDER")
    monkeypatch.delenv("CARREL_TEST_PROVIDER", raising=False)
    assert p.effective_api_key == p.api_key
    monkeypatch.setenv("CARREL_TEST_PROVIDER", "env-secret-9999")
    assert p.effective_api_key == "env-secret-9999"
    assert p.public_dict()["api_key"].endswith("9999")
    assert "env-secret" not in json.dumps(p.public_dict())
    monkeypatch.setenv("CARREL_TEST_PROVIDER", "")
    assert p.effective_api_key == "" and not p.configured
    for key in ("a", "abcd", "sk-x", "secret-abcd"):
        masked = provider(api_key=key).masked_api_key
        assert key not in masked


def test_sensitive_headers_redacted():
    p = provider(extra_headers={"Authorization": "Bearer another-secret", "x-api-key": "header-secret", "x-version": "v1"})
    data = json.dumps(p.public_dict())
    assert "another-secret" not in data and "header-secret" not in data
    assert p.public_dict()["extra_headers"]["x-version"] == "v1"
    assert "header-secret" not in p.redact("error header-secret")


@pytest.mark.parametrize("kwargs", [{"id": "../escape"}, {"id": "legacy"}, {"protocol": "bogus"},
                                   {"base_url": "https://user:secret@relay.test"}, {"base_url": "https://relay.test?key=secret"},
                                   {"extra_headers": {"x-a": "bad\r\nvalue"}}, {"pricing": {"input": -1}}])
def test_invalid_provider(kwargs):
    with pytest.raises(ValueError):
        provider(**kwargs).validate()


def test_config_loads_headers_and_registry(tmp_path, monkeypatch):
    cfgfile = tmp_path / "local.toml"
    cfgfile.write_text('[model]\nname="old"\n[model.extra_headers]\nx-version="v1"\n', encoding="utf-8")
    p = provider()
    ProviderRegistry({p.id: p}, {"motivation": RouteConfig(p.id)}, path=tmp_path / "providers.toml").save()
    cfg = load_config(data_dir=tmp_path, config_file=cfgfile)
    assert cfg.model.extra_headers == {"x-version": "v1"}
    assert cfg.providers[p.id] == p
