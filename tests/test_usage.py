"""Cost control: per-task routing, reasoning switch, accounting, usage report.

These pin down the behaviour the cost work depends on. Each test states the
bug it guards against, because every one of them was observed on real traces.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from carrel.config import DEFAULT_MAX_TOKENS, ModelConfig, TaskModelConfig, load_config
from carrel.model import ModelSchemaError, ModelTruncated, build_backend, extract_json
from carrel.model.base import MessagesBackend
from carrel.store.db import SCHEMA_VERSION, Database


# --------------------------------------------------------------------------
# Config: per-task table
# --------------------------------------------------------------------------

class TestByTaskConfig:
    def test_defaults_inherit_model_and_thinking(self):
        cfg = ModelConfig(name="glm-5.3", thinking="disabled")
        assert cfg.for_task("translate") == ("glm-5.3", "disabled", DEFAULT_MAX_TOKENS["translate"])

    def test_override_wins_field_by_field(self):
        cfg = ModelConfig(name="glm-5.3", thinking="disabled",
                          by_task={"summarize": TaskModelConfig(name="claude-opus-5-5")})
        name, thinking, ceiling = cfg.for_task("summarize")
        assert name == "claude-opus-5-5"
        assert thinking == "disabled"  # not overridden -> inherited
        assert ceiling == DEFAULT_MAX_TOKENS["summarize"]

    def test_unknown_task_has_no_ceiling(self):
        # 0 tells the backend to keep the caller's literal max_tokens.
        assert ModelConfig().for_task("probe")[2] == 0

    def test_summarize_ceiling_fits_thinking_plus_reply(self):
        # Every real summarize call hit the old 4000 cap; the new one must have
        # headroom for a 2k thinking trace and a ~3k JSON reply.
        assert DEFAULT_MAX_TOKENS["summarize"] >= 8000

    def test_toml_by_task_and_pricing(self, tmp_path):
        toml = tmp_path / "local.toml"
        toml.write_text(
            "[model]\nname = 'glm-5.3'\nthinking = 'disabled'\n"
            "[model.pricing]\ninput = 1.0\noutput = 3.0\n"
            "[model.by_task.translate]\nname = 'glm-5.3-flash'\nmax_tokens = 9000\n"
            "[model.by_task.bogus]\nname = 'x'\n",
            encoding="utf-8",
        )
        cfg = load_config(data_dir=tmp_path, config_file=toml)
        assert cfg.model.for_task("translate") == ("glm-5.3-flash", "disabled", 9000)
        assert "bogus" not in cfg.model.by_task
        assert cfg.model.pricing["input"] == 1.0 and cfg.model.pricing["output"] == 3.0
        # untouched keys keep their defaults
        assert cfg.model.pricing["cache_read"] > 0
        assert "claude-opus-5-5" in cfg.model.pricing["per_model"]

    def test_config_file_endpoint_beats_generic_anthropic_env(self, tmp_path, monkeypatch):
        # A Claude Code session exports ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN
        # for its own proxy. That proxy answered a "glm-5.3" benchmark with
        # claude-opus-5-5. The config file's endpoint and key must win over the
        # generic variables; only carrel's own variables override the file.
        toml = tmp_path / "local.toml"
        toml.write_text("[model]\nbase_url = 'http://gw.example'\napi_key = 'file-key'\n",
                        encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:15721")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "session-token")
        monkeypatch.delenv("CARREL_MODEL_BASE_URL", raising=False)
        monkeypatch.delenv("CARREL_MODEL_API_KEY", raising=False)
        cfg = load_config(data_dir=tmp_path, config_file=toml)
        assert cfg.model.base_url == "http://gw.example"
        assert cfg.model.api_key == "file-key"
        # no base_url in the file -> the generic variable is the fallback
        toml.write_text("[model]\nname = 'glm-5.3'\n", encoding="utf-8")
        cfg = load_config(data_dir=tmp_path, config_file=toml)
        assert cfg.model.base_url == "http://127.0.0.1:15721"
        assert cfg.model.api_key == "session-token"
        # carrel-specific variables override everything
        monkeypatch.setenv("CARREL_MODEL_BASE_URL", "http://override")
        monkeypatch.setenv("CARREL_MODEL_API_KEY", "override-key")
        cfg = load_config(data_dir=tmp_path, config_file=toml)
        assert (cfg.model.base_url, cfg.model.api_key) == ("http://override", "override-key")


# --------------------------------------------------------------------------
# Backend: payload shape, truncation, accounting
# --------------------------------------------------------------------------

def _anthropic_reply(text: str, *, out: int, think: int = 0, stop: str = "end_turn",
                     tin: int = 100) -> dict[str, Any]:
    return {
        "id": "msg", "type": "message", "role": "assistant", "model": "glm-5.3",
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
        "stop_reason": stop, "stop_details": None,
        "usage": {
            "input_tokens": tin, "output_tokens": out,
            "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3,
            "output_tokens_details": {"thinking_tokens": think},
        },
    }


class _Resp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def fake_post(monkeypatch):
    """Capture the payload the backend sends and script the reply."""
    sent: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = []

    def post(url, *, headers, json, timeout):  # noqa: A002 - httpx signature
        sent.append({"url": url, "payload": json})
        nxt = replies.pop(0) if replies else _anthropic_reply('{"ok": true}', out=5)
        return nxt if isinstance(nxt, _Resp) else _Resp(nxt)

    monkeypatch.setattr("carrel.model.base.httpx.post", post)
    return sent, replies


def _backend(**kw) -> MessagesBackend:
    cfg = ModelConfig(base_url="http://gw", api_key_file="k", name="glm-5.3",
                      thinking="disabled", **kw)
    be = build_backend(cfg)
    # Skip the capability probe so each test's scripted reply goes to the call
    # under test; the probe has its own tests below. Any (model, thinking) pair
    # resolves to "no native schema".
    from carrel.model import Capabilities

    class _NoProbe(dict):
        def get(self, key, default=None):
            return Capabilities(strict_schema=False, json_mode=False)

    be._caps = _NoProbe()
    return be  # type: ignore[return-value]


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
PROBE_REPLY = '{"probe_result": {"accepted": true, "label": "carrel"}}'


class TestMessagesBackend:
    def test_disabled_thinking_is_sent_as_switch(self, fake_post):
        sent, _ = fake_post
        _backend().raw_structured(schema=SCHEMA, system="s", user="u", task="translate")
        payload = sent[-1]["payload"]
        assert payload["thinking"] == {"type": "disabled"}
        assert "output_config" not in payload or "effort" not in payload["output_config"]

    def test_effort_level_is_sent_as_output_config(self, fake_post):
        sent, _ = fake_post
        _backend(by_task={"summarize": TaskModelConfig(thinking="low")}).raw_structured(
            schema=SCHEMA, system="s", user="u", task="summarize")
        payload = sent[-1]["payload"]
        assert payload["output_config"]["effort"] == "low"
        assert "thinking" not in payload

    def test_per_task_model_and_ceiling_override_caller(self, fake_post):
        sent, _ = fake_post
        be = _backend(by_task={"summarize": TaskModelConfig(name="claude-opus-5-5")})
        # caller still passes the old literal 4000; the configured ceiling wins
        be.raw_structured(schema=SCHEMA, system="s", user="u", max_tokens=4000, task="summarize")
        payload = sent[-1]["payload"]
        assert payload["model"] == "claude-opus-5-5"
        assert payload["max_tokens"] == DEFAULT_MAX_TOKENS["summarize"]

    def test_task_without_ceiling_keeps_caller_literal(self, fake_post):
        sent, _ = fake_post
        _backend().text(system="s", user="u", max_tokens=321, task="")
        assert sent[-1]["payload"]["max_tokens"] == 321

    def test_probe_runs_once_and_is_persisted(self, fake_post, tmp_path):
        sent, replies = fake_post
        caps_path = tmp_path / "model_caps.json"
        cfg = ModelConfig(base_url="http://gw", api_key_file="k", name="glm-5.3")
        be = build_backend(cfg, caps_path=caps_path)
        replies.append(_anthropic_reply(PROBE_REPLY, out=20))  # probe
        replies.append(_anthropic_reply('{"ok": true}', out=4))  # the real call
        be.raw_structured(schema=SCHEMA, system="s", user="u", task="classify")
        assert len(sent) == 2
        probe = sent[0]["payload"]
        # the probe must exercise the native schema path, or it proves nothing;
        # it runs with thinking off and room to answer so it cannot fail itself
        assert probe["output_config"]["format"]["type"] == "json_schema"
        assert probe["thinking"] == {"type": "disabled"} and probe["max_tokens"] >= 256
        assert "probe_result" not in probe["system"] + probe["messages"][0]["content"]
        assert be.capabilities().strict_schema
        assert caps_path.is_file()
        # a second backend for the same endpoint reuses the result: no probe call
        be2 = build_backend(cfg, caps_path=caps_path)
        be2.raw_structured(schema=SCHEMA, system="s", user="u", task="classify")
        assert len(sent) == 3
        assert be2.capabilities().strict_schema

    @pytest.mark.parametrize("response", [
        '{"ok": true}',
        '{"probe_result": {"accepted": true}}',
        '{"probe_result": {"accepted": true, "label": "carrel"}, "extra": 1}',
    ])
    def test_probe_rejects_ignored_or_partial_schema(self, fake_post, response):
        sent, replies = fake_post
        replies.append(_anthropic_reply(response, out=20))
        replies.append(_anthropic_reply('{"ok": true}', out=4))
        be = build_backend(ModelConfig(base_url="http://gw", api_key_file="k", name="glm-5.3"))
        be.raw_structured(schema=SCHEMA, system="s", user="u")
        assert not be.capabilities().strict_schema
        assert '"required": ["ok"]' in sent[-1]["payload"]["system"]
        assert "output_config" not in sent[-1]["payload"]

    def test_old_probe_cache_cannot_keep_false_native_support(self, fake_post, tmp_path):
        import time
        sent, replies = fake_post
        caps_path = tmp_path / "model_caps.json"
        cfg = ModelConfig(base_url="http://gw", api_key_file="k", name="glm-5.3")
        old = {"messages|http://gw|glm-5.3|disabled": {"strict_schema": True, "json_mode": True,
                                                        "probed_at": time.time()}}
        caps_path.write_text(json.dumps(old))
        replies.append(_anthropic_reply('{"ok": true}', out=4))
        be = build_backend(cfg, caps_path=caps_path)
        assert not be.capabilities().strict_schema
        assert len(sent) == 1

    def test_stale_probe_result_is_rerun(self, fake_post, tmp_path):
        sent, replies = fake_post
        caps_path = tmp_path / "model_caps.json"
        cfg = ModelConfig(base_url="http://gw", api_key_file="k", name="glm-5.3")
        old = {"messages|http://gw|glm-5.3|disabled": {"strict_schema": False, "json_mode": False,
                                                        "probed_at": 1.0,
                                                        "probe_version": MessagesBackend.CAPS_PROBE_VERSION}}
        caps_path.write_text(json.dumps(old))
        replies.append(_anthropic_reply(PROBE_REPLY, out=20))  # fresh probe
        replies.append(_anthropic_reply('{"ok": true}', out=4))
        build_backend(cfg, caps_path=caps_path).raw_structured(schema=SCHEMA, system="s", user="u", task="classify")
        assert len(sent) == 2, "a week-old negative answer is probed again"

    def test_accounting_fields_come_from_usage(self, fake_post):
        _, replies = fake_post
        replies.append(_anthropic_reply('{"ok": true}', out=900, think=600, tin=250))
        reply = _backend(pricing={"input": 2.0, "output": 10.0, "cache_read": 0.5,
                                  "cache_write": 2.5}).raw_structured(
            schema=SCHEMA, system="s", user="u", task="extract")
        assert (reply.tokens_in, reply.tokens_out, reply.thinking_tokens) == (250, 900, 600)
        assert (reply.cache_read_tokens, reply.cache_write_tokens) == (7, 3)
        assert reply.stop_reason == "end_turn" and reply.task == "extract"
        assert reply.model == "glm-5.3" and reply.thinking == "disabled"
        # 250*2 + 900*10 + 7*0.5 + 3*2.5 = 9511 micro-dollars
        assert reply.cost_usd == pytest.approx(0.009511)
        acc = reply.accounting()
        assert acc["thinking_tokens"] == 600 and "_trace" in acc and acc["truncated"] is False

    def test_truncated_reply_raises_instead_of_half_parsing(self, fake_post):
        # Real summarize calls stopped at max_tokens and were accepted as valid
        # JSON because an inner array happened to close. Now it is an error that
        # still carries the reply for accounting.
        _, replies = fake_post
        cut = '{"one_line": "x", "contributions": ["a", "b"], "tldr": "unfinished'
        replies.append(_anthropic_reply(cut, out=DEFAULT_MAX_TOKENS["summarize"], stop="max_tokens"))
        with pytest.raises(ModelTruncated) as info:
            _backend().raw_structured(schema=SCHEMA, system="s", user="u", task="summarize")
        assert info.value.reply is not None
        assert info.value.reply.tokens_out == DEFAULT_MAX_TOKENS["summarize"]
        assert info.value.reply.truncated

    def test_gateway_relabelled_stop_is_still_detected(self, fake_post):
        # The gateway reports "refusal" for normal stops; a reply that used the
        # whole budget minus one token is treated as truncated regardless.
        _, replies = fake_post
        ceiling = DEFAULT_MAX_TOKENS["repair"]
        replies.append(_anthropic_reply('{"translations": []}', out=ceiling - 1, stop="refusal"))
        with pytest.raises(ModelTruncated):
            _backend().raw_structured(schema={"type": "object", "properties": {}},
                                      system="s", user="u", task="repair")

    def test_normal_refusal_label_does_not_break_parsing(self, fake_post):
        _, replies = fake_post
        replies.append(_anthropic_reply('{"ok": true}', out=12, stop="refusal"))
        reply = _backend().raw_structured(schema=SCHEMA, system="s", user="u", task="classify")
        assert reply.data == {"ok": True} and not reply.truncated

    def test_substituted_model_is_recorded_and_billed_at_its_rate(self, fake_post):
        # Observed on the local gateway: a glm-5.3 request answered by
        # claude-opus-5-5 (response.model), with a thinking trace, for 17 minutes
        # straight. The reply must say who answered and cost what *they* cost.
        _, replies = fake_post
        r = _anthropic_reply('{"ok": true}', out=1000, think=600, tin=1000)
        r["model"] = "claude-opus-5-5"
        replies.append(r)
        reply = _backend().raw_structured(schema=SCHEMA, system="s", user="u", task="extract")
        assert reply.requested_model == "glm-5.3" and reply.model == "claude-opus-5-5"
        assert reply.substituted
        # opus rates from DEFAULT_PRICING.per_model: 1000*4 + 1000*20 + 7*0.2 + 3*5 = 24016.4 micro-USD
        assert reply.cost_usd == pytest.approx(0.024016, abs=1e-6)
        acc = reply.accounting()
        assert acc["requested_model"] == "glm-5.3" and acc["model"] == "claude-opus-5-5"

    def test_bare_array_is_returned_for_the_caller_to_normalise(self, fake_post):
        # glm-5.3 answers a glossary request with a bare array of terms. That
        # used to be scanned for the first "{" and came back as *one* term.
        _, replies = fake_post
        replies.append(_anthropic_reply('[{"term": "a", "zh": "甲"}, {"term": "b", "zh": "乙"}]', out=40))
        reply = _backend().raw_structured(schema={"type": "object", "properties": {"terms": {"type": "array"}}},
                                          system="s", user="u", task="glossary")
        assert isinstance(reply.data, list) and len(reply.data) == 2

    def test_call_json_retries_once_on_unparseable_reply_and_bills_both(self, fake_post):
        # A complete reply that is not JSON at all gets one restated-schema
        # retry, and the wasted call's tokens are folded into the accounting.
        from carrel.model.base import call_json

        sent, replies = fake_post
        replies.append(_anthropic_reply('Sure! Here is the summary you asked for.', out=300, tin=100))
        replies.append(_anthropic_reply('{"ok": true}', out=20, tin=120))
        reply = call_json(_backend(), schema=SCHEMA, system="s", user="u", task="summarize")
        assert reply.data == {"ok": True}
        assert len(sent) == 2
        assert "必须只返回一个 JSON 对象" in sent[1]["payload"]["system"]
        assert reply.tokens_out == 320 and reply.tokens_in == 220

    def test_call_json_does_not_retry_truncation(self, fake_post):
        from carrel.model.base import call_json

        sent, replies = fake_post
        replies.append(_anthropic_reply('{"ok": tr', out=DEFAULT_MAX_TOKENS["summarize"], stop="max_tokens"))
        with pytest.raises(ModelTruncated):
            call_json(_backend(), schema=SCHEMA, system="s", user="u", task="summarize")
        assert len(sent) == 1

    def test_combined_retry_cost_does_not_mark_complete_reply_truncated(self, fake_post):
        from carrel.model.base import call_json

        _, replies = fake_post
        ceiling = DEFAULT_MAX_TOKENS["summarize"]
        replies.append(_anthropic_reply('not json', out=ceiling - 100))
        replies.append(_anthropic_reply('{"ok": true}', out=200))
        reply = call_json(_backend(), schema=SCHEMA, system="s", user="u", task="summarize")
        assert reply.tokens_out == ceiling + 100
        assert not reply.truncated
        assert reply.accounting()["truncated"] is False

    def test_call_json_keeps_first_spend_when_retry_also_fails(self, fake_post):
        from carrel.model.base import call_json

        sent, replies = fake_post
        replies.append(_anthropic_reply('not json at all', out=300, tin=3000))
        replies.append(_anthropic_reply('still not json', out=200, tin=3200))
        with pytest.raises(ModelSchemaError) as info:
            call_json(_backend(), schema=SCHEMA, system="s", user="u", task="summarize")
        assert len(sent) == 2
        # both calls are on the exception's reply
        assert info.value.reply.tokens_in == 6200 and info.value.reply.tokens_out == 500

    def test_call_json_keeps_paid_reply_when_retry_has_transport_error(self):
        from carrel.model import ModelError, ModelReply
        from carrel.model.base import call_json

        class Backend:
            calls = 0

            def structured(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise ModelSchemaError("invalid JSON", ModelReply(
                        text="not JSON", tokens_in=120, tokens_out=40, cost_usd=0.01))
                raise ModelError("timed out")

        with pytest.raises(ModelError, match="timed out") as info:
            call_json(Backend(), schema=SCHEMA, system="s", user="u")
        assert info.value.reply.tokens_out == 40
        assert info.value.reply.cost_usd == 0.01

    def test_per_model_pricing_prefers_the_longest_match(self, fake_post):
        # Declaration order must not decide: "glm-5.3-flash" is a different
        # price from "glm-5.3" whichever is listed first.
        _, replies = fake_post
        r = _anthropic_reply('{"ok": true}', out=1000, tin=0)
        r["model"] = "glm-5.3-flash"
        replies.append(r)
        be = _backend(pricing={"input": 1.4, "output": 4.4,
                               "per_model": {"glm-5.3": {"output": 4.4},
                                             "glm-5.3-flash": {"output": 0.5}}})
        reply = be.raw_structured(schema=SCHEMA, system="s", user="u", task="extract")
        assert reply.cost_usd == pytest.approx(0.0005)  # 1000 * 0.5/M, not 4.4/M

    def test_openai_shape_does_not_double_bill_cached_input(self, fake_post):
        # prompt_tokens already includes cached_tokens on OpenAI-shaped usage.
        _, replies = fake_post
        r = _anthropic_reply('{"ok": true}', out=50)
        r["usage"] = {"prompt_tokens": 1000, "completion_tokens": 50,
                      "prompt_tokens_details": {"cached_tokens": 800}}
        replies.append(r)
        be = _backend(pricing={"input": 1.4, "output": 4.4, "cache_read": 0.26, "cache_write": 1.4})
        reply = be.raw_structured(schema=SCHEMA, system="s", user="u", task="extract")
        # 200 uncached * 1.4 + 800 cached * 0.26 + 50 * 4.4 = 708 micro-USD
        assert reply.cost_usd == pytest.approx(0.000708, abs=1e-6)
        assert reply.tokens_in == 1000  # reported as the provider defines it

    def test_disabled_thinking_maps_to_low_effort_on_always_on_models(self, fake_post):
        sent, _ = fake_post
        be = _backend(by_task={"classify": TaskModelConfig(name="claude-opus-5-5")})
        be.raw_structured(schema=SCHEMA, system="s", user="u", task="classify")
        payload = sent[-1]["payload"]
        assert "thinking" not in payload, "opus-5-5 returns 400 for thinking.type=disabled"
        assert payload["output_config"]["effort"] == "low"

    def test_probe_is_per_model_and_only_caches_real_answers(self, fake_post, tmp_path):
        from carrel.model import Capabilities

        sent, replies = fake_post
        caps_path = tmp_path / "caps.json"
        cfg = ModelConfig(base_url="http://gw", api_key_file="k", name="glm-5.3",
                          by_task={"classify": TaskModelConfig(name="other-model")})
        be = build_backend(cfg, caps_path=caps_path)
        # default model probe: a transport failure must NOT be cached as "no"
        replies.append(_Resp({"error": "boom"}, status=503))
        replies.append(_Resp({"error": "boom"}, status=503))  # retry
        replies.append(_anthropic_reply('{"ok": true}', out=4))  # the real call
        be.raw_structured(schema=SCHEMA, system="s", user="u", task="extract")
        saved = json.loads(caps_path.read_text()) if caps_path.exists() else {}
        assert not any("glm-5.3|disabled" in k for k in saved), "transport failure was persisted"
        # override model gets its own probe, with its own model id in the payload
        replies.append(_anthropic_reply('{"ok": true}', out=4))  # probe for other-model
        replies.append(_anthropic_reply('{"ok": true}', out=4))  # classify call
        before = len(sent)
        be.raw_structured(schema=SCHEMA, system="s", user="u", task="classify")
        probe_payload = sent[before]["payload"]
        assert probe_payload["model"] == "other-model"
        assert probe_payload["output_config"]["format"]["type"] == "json_schema"
        saved = json.loads(caps_path.read_text())
        assert any(k.endswith("|other-model|disabled") for k in saved)
        assert all("probed_at" in v for v in saved.values())

    def test_retry_after_header_is_honoured(self, monkeypatch):
        calls: list[float] = []
        monkeypatch.setattr("carrel.model.base.time.sleep", lambda s: calls.append(s))
        attempts = {"n": 0}

        def post(url, *, headers, json, timeout):  # noqa: A002
            attempts["n"] += 1
            if attempts["n"] == 1:
                r = _Resp({"error": "slow down"}, status=429)
                r.headers = {"retry-after": "7"}
                return r
            return _Resp(_anthropic_reply('{"ok": true}', out=3))

        monkeypatch.setattr("carrel.model.base.httpx.post", post)
        _backend().text(system="s", user="u")
        assert calls == [7.0]


class TestExtractJsonTruncation:
    def test_pinned_object_does_not_fall_back_to_inner_array(self):
        cut = '{"one_line": "x", "contributions": ["a", "b"], "tldr": "unfinished'
        with pytest.raises(ModelSchemaError):
            extract_json(cut, expect="object")

    def test_unpinned_shape_still_tolerant(self):
        assert extract_json('prose [1, 2] prose') == [1, 2]


# --------------------------------------------------------------------------
# Database: migration + usage report
# --------------------------------------------------------------------------

def _run(**over) -> dict[str, Any]:
    base = {"id": over.get("id", "r1"), "paper_id": "p", "task": "translate", "status": "ok",
            "tokens_in": 100, "tokens_out": 400, "thinking_tokens": 250, "cost_usd": 0.01,
            "model": "glm-5.3", "created_at": "2026-11-16T00:00:00Z"}
    base.update(over)
    return base


class TestUsageReport:
    def test_v2_database_gains_accounting_columns(self, tmp_path):
        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta VALUES ('version', '2');
                CREATE TABLE model_runs (
                  id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, task TEXT NOT NULL,
                  module TEXT DEFAULT '', status TEXT DEFAULT 'ok', backend TEXT DEFAULT '',
                  model TEXT DEFAULT '', tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
                  duration_ms INTEGER DEFAULT 0, schema_ok INTEGER DEFAULT 1, qa_ok INTEGER DEFAULT 1,
                  retry_of TEXT DEFAULT '', trace_dir TEXT DEFAULT '', error_code TEXT DEFAULT '',
                  created_at TEXT DEFAULT ''
                );
                INSERT INTO model_runs(id, paper_id, task, tokens_in, tokens_out)
                VALUES ('old', 'p', 'extract', 10, 20);
            """)
        db = Database(path)
        try:
            assert db.version == SCHEMA_VERSION
            cols = {r["name"] for r in db.query("PRAGMA table_info(model_runs)")}
            assert {"thinking_tokens", "cost_usd", "accepted", "truncated", "stop_reason"} <= cols
            old = db.get_run("old")
            assert old["tokens_out"] == 20 and old["thinking_tokens"] == 0 and old["accepted"] == -1
            db.record_run(_run(id="new", accepted=True, truncated=True))
            assert db.get_run("new")["accepted"] == 1
        finally:
            db.close()

    def test_summary_groups_by_task_and_counts_outcomes(self, tmp_path):
        db = Database(tmp_path / "app.db")
        try:
            db.record_run(_run(id="t1"))
            db.record_run(_run(id="t2", tokens_out=200, thinking_tokens=50, cost_usd=0.005))
            db.record_run(_run(id="r1", task="repair", status="ok", accepted=True, cost_usd=0.02))
            db.record_run(_run(id="r2", task="repair", status="rejected", accepted=False,
                               truncated=True, cost_usd=0.02))
            db.record_run(_run(id="g1", task="glossary", status="failed", tokens_out=0,
                               thinking_tokens=0, cost_usd=0.001, paper_id="q"))
            report = db.usage_summary()
            total = report["total"]
            assert total["calls"] == 5 and total["papers"] == 2
            assert total["tokens_out"] == 400 + 200 + 400 + 400 + 0
            assert total["thinking_tokens"] == 250 + 50 + 250 + 250
            assert total["cost_usd"] == pytest.approx(0.056)
            assert total["truncated"] == 1 and total["failed"] == 1
            assert total["accepted"] == 1 and total["rejected"] == 1
            by_task = {r["task"]: r for r in report["by_task"]}
            assert by_task["translate"]["calls"] == 2
            assert by_task["repair"]["accepted"] == 1 and by_task["repair"]["rejected"] == 1
            # scoping
            assert db.usage_summary(paper_id="q")["total"]["calls"] == 1
            assert db.usage_summary(since="2027-01-01")["total"]["calls"] == 0
        finally:
            db.close()


# --------------------------------------------------------------------------
# Pipeline: every call is recorded, including the ones that used to be invisible
# --------------------------------------------------------------------------

def _import(pipeline, tmp_path: Path, name: str = "paper.pdf") -> str:
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.4 " + name.encode())
    return pipeline.process(str(pdf))


class TestPipelineAccounting:
    def test_every_model_call_lands_in_model_runs(self, pipeline_factory, tmp_path, db):
        from tests.conftest import FakeBackend  # noqa: WPS433 - test helper

        backend = FakeBackend()
        pipeline = pipeline_factory(backend=backend)
        pid = _import(pipeline, tmp_path)
        runs = db.runs_for(pid)
        tasks = {r["task"] for r in runs}
        # preview and glossary were not recorded before; term_review is skipped
        # by the fake (empty replacements) only when the glossary is empty, and
        # the fake glossary is not empty, so it must show up too.
        assert {"preview", "extract", "summarize", "glossary", "translate",
                "classify", "assess", "term_review"} <= tasks
        # curate is no longer a model call
        assert "curate" not in tasks
        assert all(r["task"] for r in backend.calls)
        glossary = [r for r in runs if r["task"] == "glossary"][0]
        assert glossary["tokens_in"] > 0 and glossary["status"] == "ok"
        report = db.usage_summary(paper_id=pid)
        assert report["total"]["calls"] == len(runs) > 0
        assert report["total"]["cost_usd"] > 0

    def test_repair_outcome_is_visible(self, pipeline_factory, tmp_path, db):
        from tests.conftest import FakeBackend

        pipeline = pipeline_factory(backend=FakeBackend(bad_translation=True))
        pid = _import(pipeline, tmp_path, "bad.pdf")
        repairs = [r for r in db.runs_for(pid) if r["task"] == "repair"]
        assert repairs, "a bad translation must trigger repair"
        # The fake repairs with the same bad text, so nothing improves: every
        # repair is recorded as rejected, not silently as ok.
        assert {r["status"] for r in repairs} == {"rejected"}
        assert {r["accepted"] for r in repairs} == {0}

    def test_existing_glossary_is_reused_not_regenerated(self, pipeline_factory, tmp_path, db):
        from carrel.store.paths import paper_dir
        from tests.conftest import FakeBackend

        backend = FakeBackend()
        pipeline = pipeline_factory(backend=backend)
        pid = _import(pipeline, tmp_path, "reuse.pdf")
        path = paper_dir(pipeline.data_dir, pid) / "glossary.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["terms"], "first run must produce a glossary"
        saved["terms"].append({"src": "handmade", "zh": "手改", "note": "", "keep_original": False})
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

        before = sum(1 for c in backend.calls if c["task"] == "glossary")
        # a non-force rerun of a paper that is not ready yet: simulate by resetting status
        db.set_status(pid, "partial")
        pipeline.process(str(tmp_path / "reuse.pdf"))
        after = sum(1 for c in backend.calls if c["task"] == "glossary")
        assert after == before, "a non-empty glossary on disk is reused"
        kept = json.loads(path.read_text(encoding="utf-8"))
        assert any(t["src"] == "handmade" for t in kept["terms"])

    def test_force_regeneration_failure_does_not_wipe_glossary(self, pipeline_factory, tmp_path, db):
        # build_glossary seeds terms from extract definitions even when the model
        # call failed, so "non-empty" was never proof of success — and a failed
        # force rerun replaced a 38-term table with the seeds.
        from carrel.store.paths import paper_dir
        from tests.conftest import FakeBackend

        class GlossaryFails(FakeBackend):
            def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
                from carrel.model import ModelError
                if task == "glossary" and self.calls and any(c["task"] == "glossary" for c in self.calls):
                    raise ModelError("gateway hiccup")
                return super().structured(schema=schema, system=system, user=user,
                                          max_tokens=max_tokens, effort=effort, task=task)

        backend = GlossaryFails()
        pipeline = pipeline_factory(backend=backend)
        pid = _import(pipeline, tmp_path, "g.pdf")
        path = paper_dir(pipeline.data_dir, pid) / "glossary.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved["terms"].append({"src": "handmade", "zh": "手改", "note": "", "keep_original": False})
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        pipeline.process(str(tmp_path / "g.pdf"), force=True)
        kept = json.loads(path.read_text(encoding="utf-8"))
        assert any(t["src"] == "handmade" for t in kept["terms"]), "failed regeneration overwrote the table"
        assert [r for r in db.runs_for(pid) if r["task"] == "glossary" and r["status"] == "failed"]

    def test_corrupt_glossary_file_does_not_wedge_the_paper(self, pipeline_factory, tmp_path, db):
        from carrel.store.paths import paper_dir
        from tests.conftest import FakeBackend

        pipeline = pipeline_factory(backend=FakeBackend())
        pid = _import(pipeline, tmp_path, "c.pdf")
        path = paper_dir(pipeline.data_dir, pid) / "glossary.json"
        path.write_text('{"terms": [{"src": "orchestr', encoding="utf-8")  # half-written
        db.set_status(pid, "partial")
        pipeline.process(str(tmp_path / "c.pdf"))
        assert db.get_paper(pid)["status"] == "ready"
        assert json.loads(path.read_text(encoding="utf-8"))["terms"]

    def test_truncated_translate_and_extract_still_book_their_cost(self, pipeline_factory, tmp_path, db):
        from carrel.model import ModelReply, ModelTruncated
        from tests.conftest import FakeBackend

        class Truncates(FakeBackend):
            def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
                if task == "translate":
                    raise ModelTruncated("cut", ModelReply(text="", tokens_in=9000, tokens_out=12000,
                                                           stop_reason="max_tokens", max_tokens=12000,
                                                           cost_usd=0.09, task=task))
                return super().structured(schema=schema, system=system, user=user,
                                          max_tokens=max_tokens, effort=effort, task=task)

        pipeline = pipeline_factory(backend=Truncates())
        # every module truncates -> the paper legitimately fails; the point is
        # that the paid-for calls are on the ledger anyway
        from carrel.pipeline import PipelineError
        with pytest.raises(PipelineError):
            _import(pipeline, tmp_path, "t.pdf")
        pid = db.list_papers()[0]["id"]
        rows = [r for r in db.runs_for(pid) if r["task"] == "translate"]
        assert rows and all(r["status"] == "truncated" for r in rows)
        assert all(r["tokens_out"] == 12000 and r["truncated"] == 1 for r in rows)
        by_task = {r["task"]: r for r in db.usage_summary(paper_id=pid)["by_task"]}
        assert by_task["translate"]["truncated"] == len(rows)
        assert by_task["translate"]["cost_usd"] == pytest.approx(0.09 * len(rows))

    def test_assess_is_booked_even_when_classify_fails(self, pipeline_factory, tmp_path, db):
        from carrel.pipeline import PipelineError
        from tests.conftest import FakeBackend

        class ClassifyFails(FakeBackend):
            def structured(self, *, schema, system, user, max_tokens=8000, effort="", task=""):
                from carrel.model import ModelError
                if task == "classify":
                    raise ModelError("boom")
                return super().structured(schema=schema, system=system, user=user,
                                          max_tokens=max_tokens, effort=effort, task=task)

        pipeline = pipeline_factory(backend=ClassifyFails())
        with pytest.raises(PipelineError):
            _import(pipeline, tmp_path, "cf.pdf")
        pid = db.list_papers()[0]["id"]
        tasks = [r["task"] for r in db.runs_for(pid)]
        assert "assess" in tasks, "the concurrent, paid assess call vanished from the ledger"
