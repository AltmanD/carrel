import pytest

from carrel.config import ModelConfig
from carrel.model.base import build_backend


@pytest.mark.parametrize("protocol", ["messages", "chat-completions", "responses"])
def test_protocol_headers_and_case_insensitive_overrides(protocol):
    be = build_backend(ModelConfig(protocol=protocol, base_url="http://test", resolved_api_key="secret"))
    headers = be._headers()
    assert headers["authorization"] == "Bearer secret"
    assert ("x-api-key" in headers) == (protocol == "messages")
    assert ("anthropic-version" in headers) == (protocol == "messages")
    be = build_backend(ModelConfig(protocol=protocol, base_url="http://test", resolved_api_key="secret",
                                    extra_headers={"Authorization": "custom", "X-Custom": "value"}))
    assert be._headers()["authorization"] == "custom"
    assert be._headers()["x-custom"] == "value"
    assert "Authorization" not in be._headers()


def test_responses_payload_and_accounting(monkeypatch):
    from carrel.model.base import Capabilities
    sent = []
    be = build_backend(ModelConfig(protocol="responses", name="gpt-test", base_url="https://test/v1", resolved_api_key="s"))
    monkeypatch.setattr(be, "capabilities", lambda **kw: Capabilities(strict_schema=True))
    def send(payload):
        sent.append(payload)
        return {"model": "gpt-test", "status": "completed", "usage": {
            "input_tokens": 100, "output_tokens": 25, "input_tokens_details": {"cached_tokens": 40},
            "output_tokens_details": {"reasoning_tokens": 10}}}, '{"ok": true}'
    monkeypatch.setattr(be, "_send", send)
    reply = be.raw_structured(schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                                      "required": ["ok"], "additionalProperties": False}, system="s", user="u")
    assert sent[0]["text"]["format"]["type"] == "json_schema"
    assert sent[0]["max_output_tokens"] == 8000 and "messages" not in sent[0]
    assert reply.tokens_in == 100 and reply.thinking_tokens == 10 and reply.cache_read_tokens == 40
    assert reply.requested_model == "gpt-test" and not reply.substituted
    assert be._extract_text({"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}) == "ok"


@pytest.mark.parametrize("base,expected", [
    ("https://test", "https://test/v1/responses"),
    ("https://test/v1", "https://test/v1/responses"),
    ("https://test/coding/v3", "https://test/coding/v3/responses"),
    ("https://test/v1/responses", "https://test/v1/responses"),
])
def test_endpoint_prefixes(base, expected):
    be = build_backend(ModelConfig(protocol="responses", base_url=base))
    assert be._request_url("/v1/responses") == expected


def test_responses_incomplete_and_strict_nested_schema():
    be = build_backend(ModelConfig(protocol="responses", base_url="https://test"))
    reply = be._final(None, "", {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}})
    assert reply.truncated
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"value": {"type": "string"}}}}}}
    payload = be._payload(model="gpt-test", system="s", user="u", max_tokens=1000, thinking="low", schema=schema, strict=True)
    strict = payload["text"]["format"]["schema"]
    assert strict["required"] == ["items"]
    assert strict["properties"]["items"]["items"]["required"] == ["value"]
    assert "required" not in schema
