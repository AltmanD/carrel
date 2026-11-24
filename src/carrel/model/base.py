"""Protocol-level model backends.

The app talks to a *protocol*, not a vendor. Three wire shapes are supported:

* ``messages``          -> POST /v1/messages
* ``chat-completions``  -> POST /v1/chat/completions
* ``responses``         -> POST /v1/responses

Structured output support differs wildly between compatible endpoints, so the
backend probes its own capability once and always re-validates locally.

Architecture rule: every task in this app is a single-turn request — one
system prompt, one user prompt, one reply, sent straight to the endpoint with
``httpx``. There is no agent loop, no tool use and no conversation history, and
the backend must stay that way. If a future feature genuinely needs multi-turn
interaction, it keeps its own history and lives in its own module; it does not
get bolted onto these backends. Per-task model selection, reasoning control and
output ceilings are resolved from ``ModelConfig.by_task`` here, so this module
is the one place that decides what a call costs.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from contextvars import ContextVar
from carrel.runtime.retry import RetryPolicy, with_retry, classify, check_response
from carrel.runtime.locks import cache_locks, path_key
from carrel.store.paths import atomic_write_json


class ModelError(RuntimeError):
    """Base class for model failures."""

    code = "model_error"


class ModelAuthError(ModelError):
    code = "model_auth_error"


class ModelSchemaError(ModelError):
    """The reply came back but was not usable JSON / did not match the schema.

    Carries the reply when there is one, so the caller can still record the
    tokens that were paid for and show the actual text in the run trace.
    """

    code = "model_schema_invalid"

    def __init__(self, message: str, reply: "ModelReply | None" = None) -> None:
        super().__init__(message)
        self.reply = reply


class ModelTruncated(ModelError):
    """The reply stopped at ``max_tokens``; what came back is incomplete.

    Carries the partial reply so the caller can still account for the tokens
    it paid for (and decide whether a bigger ceiling is worth a retry).
    """

    code = "model_truncated"

    def __init__(self, message: str, reply: "ModelReply | None" = None) -> None:
        super().__init__(message)
        self.reply = reply


@dataclass(slots=True)
class Capabilities:
    strict_schema: bool = False
    json_mode: bool = False
    vision: bool = False
    max_output: int = 8192
    leaves_machine: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strict_schema": self.strict_schema,
            "json_mode": self.json_mode,
            "vision": self.vision,
            "max_output": self.max_output,
            "leaves_machine": self.leaves_machine,
        }


@dataclass(slots=True)
class ModelReply:
    text: str
    data: Any = None
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    # The exact prompts sent, kept so a run's trace page can show the real
    # interaction rather than a summary of it.
    system_prompt: str = ""
    user_prompt: str = ""
    # Accounting detail. Reasoning tokens are part of ``tokens_out`` and billed
    # as output; they are broken out here because they were the majority of the
    # bill and are invisible otherwise.
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""
    max_tokens: int = 0
    #: The model named in the response — what actually answered and what is billed.
    model: str = ""
    #: The model the request asked for; differs from ``model`` when the gateway
    #: substituted another one.
    requested_model: str = ""
    task: str = ""
    thinking: str = ""
    cost_usd: float = 0.0
    # Retry accounting includes earlier calls; truncation belongs to the final
    # response, not their combined output token count.
    final_tokens_out: int | None = None
    #: The provider selected by task routing, when one is active.
    provider: str = ""

    @property
    def truncated(self) -> bool:
        """True when the reply hit its output ceiling.

        ``stop_reason`` is the honest signal, but the gateway in front of this
        app relabels most stops as ``refusal``, so a reply that used the whole
        budget (minus one token of slack) is treated as truncated too.
        """
        if self.stop_reason in ("max_tokens", "length", "max_output_tokens"):
            return True
        output = self.tokens_out if self.final_tokens_out is None else self.final_tokens_out
        return bool(self.max_tokens) and output >= self.max_tokens - 1

    @property
    def substituted(self) -> bool:
        """The gateway answered with a different model than was requested."""
        return bool(self.requested_model and self.model and self.model != self.requested_model)

    def accounting(self) -> dict[str, Any]:
        """The fields a run record needs; merged into ``meta`` by callers."""
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "duration_ms": self.duration_ms,
            "thinking_tokens": self.thinking_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "stop_reason": self.stop_reason,
            "max_tokens": self.max_tokens,
            "model": self.model,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "thinking": self.thinking,
            "truncated": self.truncated,
            "cost_usd": self.cost_usd,
            "_trace": {"system": self.system_prompt, "user": self.user_prompt,
                       "response": self.raw, "text": self.text},
        }


class ModelBackend(Protocol):
    name: str
    protocol: str

    def structured(
        self,
        *,
        schema: dict[str, Any],
        system: str,
        user: str,
        max_tokens: int = 8000,
        effort: str = "medium",
        task: str = "",
    ) -> ModelReply: ...

    def text(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 8000,
        effort: str = "medium",
        task: str = "",
    ) -> ModelReply: ...

    def capabilities(self) -> Capabilities: ...


# --------------------------------------------------------------------------
# JSON schema helpers
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")


def _repair_escapes(text: str) -> str:
    """Make backslashes inside JSON strings legal.

    Models writing LaTeX inside JSON strings emit ``\\theta``, ``\\$0.21``,
    ``\\left[`` — a backslash followed by something that is not a JSON escape.
    The intent is always a literal backslash, so each such one is doubled. A
    plain regex cannot do this: ``\\\\{`` is a legal escaped backslash followed
    by ``{``, while ``\\{`` is not, and only a left-to-right scan that consumes
    complete escapes tells them apart.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if not in_str:
            if ch == '"':
                in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = False
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt in '"\\/bfnrt':
                out.append(ch + nxt)
                i += 2
                continue
            if nxt == "u" and _HEX4.match(text[i + 2 : i + 6] or ""):
                out.append(text[i : i + 6])
                i += 6
                continue
            out.append("\\\\")  # lone backslash -> literal
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _loads_lenient(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_repair_escapes(text))


def extract_json(text: str, expect: str | None = None) -> Any:
    """Pull the first JSON value out of a model reply.

    Compatible endpoints routinely wrap JSON in prose or fences even when asked
    not to, so this is deliberately forgiving about the envelope and strict
    about the payload (the caller validates the schema).

    ``expect`` ("object" or "array") selects which opening bracket to look for.
    This matters: a model asked for an object sometimes answers with a bare array
    of the nested items, and naively scanning for ``{`` first would then parse
    one inner element and fail the schema for no useful reason.
    """
    text = (text or "").strip()
    if not text:
        raise ModelSchemaError("empty response")
    if m := _FENCE.search(text):
        text = m.group(1).strip()

    # If the whole reply *is* one JSON value, take it as-is, whatever shape was
    # asked for: a bare array of items where an object was expected is a shape
    # the callers normalise. Scanning for the expected bracket first would land
    # on the first *element* of that array and hand back one term out of forty.
    if text[:1] in "[{":
        closer = "]" if text[0] == "[" else "}"
        if text.endswith(closer):
            try:
                return _loads_lenient(text)
            except json.JSONDecodeError:
                pass  # genuinely broken somewhere; fall through to the scanner

    order = [("{", "}"), ("[", "]")]
    if expect == "object":
        order = [("{", "}")]
    elif expect == "array":
        order = [("[", "]")]

    for opener, closer in order:
        value = _balanced_json(text, opener, closer)
        if value is not _MISSING:
            return value
    # Last resort: try the opposite shape rather than giving up outright — but
    # only when the caller did not pin the shape. A truncated object reply still
    # contains complete inner arrays, and returning one of those as "the result"
    # is how a cut-off summary used to be silently accepted as an empty one.
    if expect is None:
        for opener, closer in [("[", "]"), ("{", "}")]:
            value = _balanced_json(text, opener, closer)
            if value is not _MISSING:
                return value
    raise ModelSchemaError("response is not JSON")


def _balanced_json(text: str, opener: str, closer: str) -> Any:
    """First balanced ``opener…closer`` span that parses as JSON.

    Spans that parse to an *empty* container are skipped when the text is
    clearly longer than that: ``{}`` from a stray ``\\tag{2}`` inside a long
    reply is not the answer, and accepting it once replaced nine translated
    paragraphs with nothing.
    """
    start = text.find(opener)
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        value = _loads_lenient(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if not value and len(text) > i + 1 + 20:
                        break  # an empty container is not the reply
                    return value
        start = text.find(opener, start + 1)
    return _MISSING


_MISSING = object()


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def call_json(
    backend: "ModelBackend",
    *,
    schema: dict[str, Any],
    system: str,
    user: str,
    max_tokens: int = 8000,
    task: str = "",
    retry_on_shape: bool = True,
) -> "ModelReply":
    """Ask the model for JSON and return the reply (``.data`` is parsed JSON).

    Compatible endpoints advertise schema support and then answer with their own
    key names or a differently-shaped envelope. Enforcing the schema at this layer
    throws away usable replies, so callers normalise the payload themselves and
    apply their own checks afterwards. The full reply — including the prompts
    actually sent — comes back so the run trace can record it.

    ``task`` names the pipeline step; the backend uses it to pick the per-task
    model, reasoning setting and output ceiling (``max_tokens`` is only the
    fallback when no per-task ceiling is configured).

    When the reply is complete but not the requested shape at all (a bare array
    where an object was asked for — observed on roughly one summary call in
    three), one retry restates the schema. A truncated reply is *not* retried
    here: that is a ceiling problem, and the caller decides.
    """
    pin = getattr(backend, "pin", None)
    if callable(pin):
        backend = pin(task)
    raw_fn = getattr(backend, "raw_structured", None)
    call = raw_fn if callable(raw_fn) else backend.structured
    try:
        return call(schema=schema, system=system, user=user, max_tokens=max_tokens, task=task)
    except ModelSchemaError as exc:
        if not retry_on_shape or getattr(exc, "reply", None) is None:
            raise
        wasted = exc.reply
        strengthened = (
            system
            + "\n\n你必须只返回一个 JSON 对象（不是数组、不是字符串），"
              "且必须严格符合下面的结构：\n"
            + json.dumps(schema, ensure_ascii=False)
            + f"\n\n上一次的回复无法解析：{str(exc)[:120]}"
        )
        try:
            reply = call(schema=schema, system=strengthened, user=user,
                         max_tokens=max_tokens, task=task)
        except ModelError as exc2:
            # Both calls were paid for. If the retry's own reply survived, fold
            # the first call into it; otherwise hand the first call's reply up
            # so at least that spend reaches the run log.
            second = getattr(exc2, "reply", None)
            if second is not None:
                _fold_spend(second, wasted)
            else:
                exc2.reply = wasted
            raise
        _fold_spend(reply, wasted)
        reply.raw = {**reply.raw, "_retried_after": wasted.text[:400]}
        return reply


def _fold_spend(into: "ModelReply", wasted: "ModelReply") -> None:
    """Add a wasted call's tokens and cost to the reply that superseded it."""
    if into.final_tokens_out is None:
        into.final_tokens_out = into.tokens_out
    into.tokens_in += wasted.tokens_in
    into.tokens_out += wasted.tokens_out
    into.thinking_tokens += wasted.thinking_tokens
    into.cache_read_tokens += wasted.cache_read_tokens
    into.cache_write_tokens += wasted.cache_write_tokens
    into.duration_ms += wasted.duration_ms
    into.cost_usd = round(into.cost_usd + wasted.cost_usd, 6)


def pick(data: Any, *keys: str, default: Any = None) -> Any:
    """First non-empty value among ``keys`` in a dict payload."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def as_list(value: Any) -> list[Any]:
    """Coerce a model field that should be a list but may be a string."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def coerce_object(data: Any, schema: dict[str, Any]) -> Any:
    """Unwrap a single-element list when the schema asked for an object.

    Models sometimes answer with ``[{...}]`` when asked for ``{...}``; the payload
    is otherwise correct and rejecting it wastes a whole call.
    """
    if schema.get("type") != "object" or not isinstance(data, list):
        return data
    if len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    # A list whose only object-valued key matches the schema's property names.
    props = set(schema.get("properties", {}))
    for item in data:
        if isinstance(item, dict) and props & set(item):
            return item
    return data


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate against the subset of JSON Schema this app actually emits."""
    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_type_ok(value, t) for t in expected):
            return [f"{path}: expected one of {expected}, got {type(value).__name__}"]
    elif expected and not _type_ok(value, expected):
        return [f"{path}: expected {expected}, got {type(value).__name__}"]

    if expected == "object" or isinstance(value, dict):
        if not isinstance(value, dict):
            return errors
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required field")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                errors.extend(validate_schema(value[key], sub, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            for key in sorted(extra):
                errors.append(f"{path}.{key}: unexpected field")
    elif expected == "array" or isinstance(value, list):
        if not isinstance(value, list):
            return errors
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                errors.extend(validate_schema(item, item_schema, f"{path}[{i}]"))
    elif expected == "string" and isinstance(value, str):
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: {value!r} not in enum")
    return errors


# --------------------------------------------------------------------------
# Shared HTTP behaviour
# --------------------------------------------------------------------------

class _HttpMixin:
    name: str = "backend"
    protocol: str = "messages"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: int = 300,
        extra_headers: dict[str, str] | None = None,
        max_retries: int = 2,
        retry_backoff: float = 2.0,
        thinking: str = "disabled",
        pricing: dict[str, float] | None = None,
        by_task: dict[str, tuple[str, str, int]] | None = None,
        caps_path: "Path | None" = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.thinking = thinking
        self.pricing = dict(pricing or {})
        # task -> (model, thinking, max_tokens); resolved once from config so
        # every call site just names its task and gets the right knobs.
        self.by_task = dict(by_task or {})
        self._caps: dict[str, Capabilities] = {}
        self._cap_error = ContextVar("capability_error", default="")
        self.resource = None
        self.retry_policy = None
        self._caps_path = caps_path
        self._extra_headers = extra_headers or {}

    # ----------------------------------------------------------- per-task
    def _resolve(self, task: str, max_tokens: int) -> tuple[str, str, int]:
        """``(model, thinking, max_tokens)`` for this call.

        A configured per-task ceiling wins over the caller's literal because the
        caller's number predates thinking-as-output billing; a task with no
        configured ceiling keeps the literal.
        """
        model, thinking, ceiling = self.by_task.get(task) or (self.model, self.thinking, 0)
        return model or self.model, thinking or self.thinking, ceiling or max_tokens

    def _cost(self, tokens_in: int, tokens_out: int,
              cache_read: int = 0, cache_write: int = 0, *, model: str = "",
              cache_in_input: bool = False) -> float:
        """USD for one call.

        ``pricing`` may be flat (one model) or keyed by model name under
        ``per_model``; the entry whose name is the longest match for the
        answering model wins (exact first), so ``glm-5.3-flash`` is not billed
        at the ``glm-5.3`` rate because that key happened to be listed first.
        ``cache_in_input`` says the usage block counted cached tokens inside
        ``tokens_in`` (OpenAI shapes); Anthropic reports them separately.
        """
        p = self.pricing
        if not p:
            return 0.0
        per_model = p.get("per_model") or {}
        if model and isinstance(per_model, dict):
            rates = per_model.get(model)
            if not isinstance(rates, dict):
                candidates = [k for k, v in per_model.items()
                              if k and k in model and isinstance(v, dict)]
                rates = per_model[max(candidates, key=len)] if candidates else None
            if isinstance(rates, dict):
                p = {**p, **rates}
        billable_in = max(0, tokens_in - cache_read) if cache_in_input else tokens_in
        return round(
            (billable_in * float(p.get("input", 0.0))
             + tokens_out * float(p.get("output", 0.0))
             + cache_read * float(p.get("cache_read", 0.0))
             + cache_write * float(p.get("cache_write", 0.0))) / 1_000_000,
            6,
        )

    # --------------------------------------------------------------- auth
    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.protocol == "messages":
            headers["anthropic-version"] = "2023-06-01"
            if self.api_key:
                headers["x-api-key"] = self.api_key
                # Existing relay endpoints accept Bearer as well as the native
                # Anthropic header, so keep both for the Messages protocol.
                headers["authorization"] = f"Bearer {self.api_key}"
        elif self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        headers.update({k.lower(): v for k, v in self._extra_headers.items()})
        return headers

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._request_url(path)
        started = time.time()
        def attempt():
            resp = httpx.post(url, headers=self._headers(), json=payload, timeout=self.timeout_s)
            check_response(resp)
            return resp
        try:
            resp = with_retry(attempt, self.retry_policy or RetryPolicy(self.max_retries + 1, self.retry_backoff),
                              resource=self.resource)
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (401, 403):
                error = ModelAuthError(f'auth rejected ({status})')
            elif status:
                error = ModelError(f'HTTP {status}: {exc.response.text[:300]}')
            else:
                error = ModelError(f'request timed out after {self.timeout_s}s' if isinstance(exc, httpx.TimeoutException)
                                   else f'request failed: {type(exc).__name__}')
            error.retryable = classify(exc) == 'retry'
            raise error from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelError('non-JSON response') from exc
        data['_duration_ms'] = int((time.time() - started) * 1000)
        return data

    def _request_url(self, path: str) -> str:
        resource = path.removeprefix("/v1")
        if self.base_url.endswith(resource):
            return self.base_url
        # A supplied path is an explicit API prefix, including /v1 or a
        # vendor's dedicated coding-plan prefix. Only bare origins need /v1.
        suffix = resource if urlsplit(self.base_url).path.rstrip("/") else path
        return self.base_url + suffix

    #: A persisted probe result older than this is re-run. A negative answer
    #: ("no native schema mode") should not outlive a gateway upgrade forever.
    CAPS_MAX_AGE_S = 7 * 24 * 3600
    CAPS_PROBE_VERSION = 2

    def capabilities(self, *, model: str = "", thinking: str = "") -> Capabilities:
        """Native-schema support for the model/thinking pair a call will use.

        Probed once per pair per data dir and persisted; the default model is
        the one ``carrel health`` reports. A by_task override may route a task
        to a model with different support, so the pair, not the backend, is
        the unit — a strict payload sent to a model that rejects it is a hard
        400 with no fallback.
        """
        key = self._caps_key(model or self.model, thinking or self.thinking)
        with cache_locks.hold(('probe', path_key(self._caps_path) if self._caps_path else '', key)):
            caps = self._caps.get(key) or self._load_caps(key)
            if caps is None:
                caps, cacheable = self._probe(model=model or self.model, thinking=thinking or self.thinking)
                if cacheable:
                    self._store_caps(key, caps)
            if not caps.error:
                self._caps[key] = caps
            self._cap_error.set(caps.error)
            return caps

    @property
    def capability_error(self):
        # Compatibility for health integrations; never shared between threads.
        return self._cap_error.get()

    @capability_error.setter
    def capability_error(self, value):
        self._cap_error.set(value)

    def _caps_key(self, model: str, thinking: str) -> str:
        return f"{self.protocol}|{self.base_url}|{model}|{thinking}"

    def _load_caps(self, key: str) -> Capabilities | None:
        """Reuse a probe result written by an earlier process, if recent enough."""
        if self._caps_path is None or not self._caps_path.is_file():
            return None
        try:
            saved = json.loads(self._caps_path.read_text(encoding="utf-8"))
            entry = saved.get(key)
        except (ValueError, OSError):
            return None
        if not isinstance(entry, dict) or entry.get("probe_version") != self.CAPS_PROBE_VERSION:
            return None
        probed_at = float(entry.get("probed_at") or 0)
        if not probed_at or time.time() - probed_at > self.CAPS_MAX_AGE_S:
            return None
        return Capabilities(
            strict_schema=bool(entry.get("strict_schema")),
            json_mode=bool(entry.get("json_mode")),
        )

    def _store_caps(self, key: str, caps: Capabilities) -> None:
        if self._caps_path is None:
            return
        with cache_locks.hold(('caps-file', path_key(self._caps_path))):
            try:
                saved = {}
                if self._caps_path.is_file():
                    saved = json.loads(self._caps_path.read_text(encoding='utf-8')) or {}
                saved[key] = {**caps.to_dict(), 'probed_at': time.time(), 'probe_version': self.CAPS_PROBE_VERSION}
                atomic_write_json(self._caps_path, saved)
            except (ValueError, OSError):
                pass

    def _probe(self, *, model: str, thinking: str) -> tuple[Capabilities, bool]:
        """Require fields that appear only in the native response schema.

        Returns ``(caps, cacheable)``. Compatible stations often accept the
        schema parameter and then ignore it, so native support is only assumed
        after a reply that validates. Giving the desired JSON in the prompt
        would let a gateway pass while dropping the schema entirely.
        Only a real answer is cacheable: a reply
        that came back but did not validate is a genuine "no", whereas a
        timeout, a 5xx or an auth failure says nothing about the model and must
        not be written down as "no native schema" for the next week. The probe
        runs with thinking off and a budget the reply cannot exhaust, so the
        probe cannot fail itself.
        """
        caps = Capabilities()
        schema = {
            "type": "object",
            "properties": {"probe_result": {
                "type": "object",
                "properties": {"accepted": {"type": "boolean", "enum": [True]},
                               "label": {"type": "string", "enum": ["carrel"]}},
                "required": ["accepted", "label"], "additionalProperties": False,
            }},
            "required": ["probe_result"],
            "additionalProperties": False,
        }
        try:
            reply = self._structured_native(
                schema=schema, system="Reply with JSON only.",
                user="Return a JSON object following the supplied response schema, using its required property names and values.",
                max_tokens=512,
                model=model, thinking="disabled",
            )
            parsed = extract_json(reply.text)
        except ModelSchemaError as exc:
            # The endpoint answered; it just did not honour the schema.
            return caps, getattr(exc, "reply", None) is not None
        except ModelError as exc:
            caps.error = str(exc)
            return caps, False
        if not validate_schema(parsed, schema) and not reply.truncated and not reply.substituted:
            caps.strict_schema = True
            caps.json_mode = True
        return caps, True

    def _structured_native(self, *, schema: dict[str, Any], system: str, user: str,
                           max_tokens: int, model: str = "", thinking: str = "") -> ModelReply:
        """One request with the protocol's native schema mode forced on."""
        payload = self._payload(model=model or self.model, system=system, user=user,
                                max_tokens=max_tokens, thinking=thinking or self.thinking,
                                schema=schema, strict=True)
        raw, text = self._send(payload)
        reply = self._reply(None, text, raw, payload, task="probe",
                            thinking=thinking or self.thinking)
        try:
            reply.data = coerce_object(extract_json(text, expect="object"), schema)
        except ModelSchemaError as exc:
            raise ModelSchemaError(str(exc), reply) from None
        return reply

    # ------------------------------------------------- protocol hooks
    # Each wire format implements these three; everything below is shared.
    _endpoint: str = ""

    def _payload(self, *, model: str, system: str, user: str, max_tokens: int,
                 thinking: str, schema: dict[str, Any] | None, strict: bool) -> dict[str, Any]:
        raise NotImplementedError

    def _extract_text(self, raw: dict[str, Any]) -> str:
        raise NotImplementedError

    def _sent_prompts(self, payload: dict[str, Any]) -> tuple[str, str, int]:
        """``(system, user, max_tokens)`` as actually sent, for the trace."""
        raise NotImplementedError

    # ------------------------------------------------- shared call path
    def _send(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        raw = self._post(self._endpoint, payload)
        return raw, self._extract_text(raw)

    def _reply(self, parsed: Any, text: str, raw: dict[str, Any], payload: dict[str, Any],
               *, task: str, thinking: str) -> ModelReply:
        system, user, max_tokens = self._sent_prompts(payload)
        return self._final(parsed, text, raw, system=system, user=user,
                           model=payload["model"], task=task, thinking=thinking,
                           max_tokens=max_tokens)

    def _call(self, *, schema: dict[str, Any] | None, system: str, user: str,
              max_tokens: int, effort: str, task: str) -> ModelReply:
        """Resolve per-task knobs, send, and return the reply with no parsing.

        Raises :class:`ModelTruncated` (carrying the reply) when the answer hit
        its ceiling, so no caller can mistake half a JSON document for a whole one.
        """
        model, thinking, ceiling = self._resolve(task, max_tokens)
        thinking = effort or thinking
        strict = bool(schema) and self.capabilities(model=model, thinking=thinking).strict_schema
        payload = self._payload(model=model, system=system, user=user, max_tokens=ceiling,
                                thinking=thinking, schema=schema, strict=strict)
        raw, text = self._send(payload)
        reply = self._reply(None, text, raw, payload, task=task, thinking=thinking)
        if reply.truncated:
            raise ModelTruncated(
                f"reply stopped at max_tokens={ceiling} ({reply.tokens_out} out, "
                f"{reply.thinking_tokens} thinking)", reply)
        return reply

    def text(self, *, system: str, user: str, max_tokens: int = 8000,
             effort: str = "", task: str = "") -> ModelReply:
        return self._call(schema=None, system=system, user=user, max_tokens=max_tokens,
                          effort=effort, task=task)

    def raw_structured(self, *, schema: dict[str, Any], system: str, user: str,
                       max_tokens: int = 8000, effort: str = "", task: str = "") -> ModelReply:
        """Ask for JSON, parse it, but do **not** enforce the schema.

        Endpoints advertise strict schema support and then return their own key
        names. Validating here would discard an otherwise usable reply, so the
        caller normalises the payload instead. ``data`` is whatever JSON parsed.
        A parse failure raises :class:`ModelSchemaError` *with* the reply, so the
        paid-for tokens still reach the run log and ``call_json`` can retry.
        """
        reply = self._call(schema=schema, system=system, user=user, max_tokens=max_tokens,
                           effort=effort, task=task)
        try:
            reply.data = coerce_object(extract_json(reply.text, expect=schema.get("type")), schema)
        except ModelSchemaError as exc:
            raise ModelSchemaError(str(exc), reply) from None
        return reply

    def structured(self, *, schema: dict[str, Any], system: str, user: str,
                   max_tokens: int = 8000, effort: str = "", task: str = "") -> ModelReply:
        reply = self.raw_structured(schema=schema, system=system, user=user,
                                    max_tokens=max_tokens, effort=effort, task=task)
        errors = validate_schema(reply.data, schema)
        if errors:
            raise ModelSchemaError("; ".join(errors[:5]), reply)
        return reply

    def structured_with_fallback(
        self,
        *,
        schema: dict[str, Any],
        system: str,
        user: str,
        max_tokens: int = 8000,
        effort: str = "",
        task: str = "",
    ) -> ModelReply:
        """Structured call that retries once with the schema spelled out.

        Some gateways accept the native schema mode and then ignore it. When the
        first reply cannot be validated, a second attempt states the schema in the
        prompt, which costs one extra call but rescues the section.
        """
        try:
            return self.structured(
                schema=schema, system=system, user=user,
                max_tokens=max_tokens, effort=effort, task=task,
            )
        except ModelSchemaError as exc:
            strengthened = (
                system
                + "\n\n你必须只返回一个 JSON 对象（不是数组、不是字符串），"
                  "且必须严格符合下面的结构：\n"
                + json.dumps(schema, ensure_ascii=False)
                + f"\n\n上一次的回复无法解析：{exc}"
            )
            return self.structured(
                schema=schema, system=strengthened, user=user,
                max_tokens=max_tokens, effort=effort, task=task,
            )

    # ------------------------------------------------------------- helpers
    def _final(self, parsed: Any, reply_text: str, raw: dict[str, Any],
               *, system: str = "", user: str = "", model: str = "",
               task: str = "", thinking: str = "", max_tokens: int = 0) -> ModelReply:
        usage = raw.get("usage") or {}
        # Anthropic shape first, OpenAI shape as fallback — the gateway may wrap
        # either kind of upstream behind either endpoint. The shapes disagree on
        # whether cached input is counted inside the input total: Anthropic's
        # ``input_tokens`` excludes ``cache_read_input_tokens``; OpenAI's
        # ``prompt_tokens`` (and Responses' ``input_tokens`` with
        # ``input_tokens_details``) include ``cached_tokens``.
        openai_shape = "prompt_tokens" in usage and "input_tokens" not in usage
        responses_shape = isinstance(usage.get("input_tokens_details"), dict)
        tokens_in = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
        thinking_tokens = int(details.get("thinking_tokens") or details.get("reasoning_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens")
                         or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                         or (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        stop = raw.get("stop_reason")
        if stop is None:
            choices = raw.get("choices") or []
            stop = (choices[0].get("finish_reason") if choices else None) or raw.get("status") or ""
        if stop == "incomplete":
            stop = (raw.get("incomplete_details") or {}).get("reason") or stop
        # The model that *answered*, which the gateway may silently substitute
        # for the one requested (observed: every glm-5.3 request in one 17-minute
        # window came back as claude-opus-5-5, with a thinking trace and at
        # eleven times the output price). Bill the one that answered.
        answered = str(raw.get("model") or model or self.model)
        requested = model or self.model
        return ModelReply(
            text=reply_text,
            data=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=int(raw.get("_duration_ms") or 0),
            raw={k: v for k, v in raw.items() if not k.startswith("_")},
            system_prompt=system,
            user_prompt=user,
            thinking_tokens=thinking_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            stop_reason=str(stop or ""),
            max_tokens=max_tokens,
            model=answered,
            requested_model=requested,
            task=task,
            thinking=thinking,
            cost_usd=self._cost(tokens_in, tokens_out, cache_read, cache_write, model=answered,
                                cache_in_input=openai_shape or responses_shape),
        )


class MessagesBackend(_HttpMixin):
    """Anthropic Messages wire format."""

    protocol = "messages"
    _endpoint = "/v1/messages"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.name = f"messages:{self.model}"

    def _payload(self, *, model: str, system: str, user: str, max_tokens: int,
                 thinking: str, schema: dict[str, Any] | None, strict: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # ``disabled`` is the cheap path: no reasoning trace billed as output.
        # Anything else is an effort level, which is the only reasoning control
        # on models whose thinking cannot be switched off — and on those models
        # the off switch is a 400, so it is translated to the lowest effort
        # instead of taking the whole task down.
        if thinking == "disabled" and _thinking_always_on(model):
            thinking = "low"
        if thinking == "disabled":
            payload["thinking"] = {"type": "disabled"}
        elif thinking:
            payload["output_config"] = {"effort": thinking}
        if schema and strict:
            payload.setdefault("output_config", {})["format"] = {
                "type": "json_schema", "schema": schema,
            }
        if schema and not strict:
            payload["system"] = (
                system
                + "\n\nReturn only a JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        return payload

    def _extract_text(self, raw: dict[str, Any]) -> str:
        return _join_text_blocks(raw)

    def _sent_prompts(self, payload: dict[str, Any]) -> tuple[str, str, int]:
        return payload.get("system", ""), payload["messages"][0]["content"], payload["max_tokens"]


class ChatCompletionsBackend(_HttpMixin):
    """OpenAI-compatible chat completions wire format."""

    protocol = "chat-completions"
    _endpoint = "/v1/chat/completions"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.name = f"chat-completions:{self.model}"

    def _payload(self, *, model: str, system: str, user: str, max_tokens: int,
                 thinking: str, schema: dict[str, Any] | None, strict: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # OpenAI-style endpoints take ``reasoning_effort``; the thinking-off
        # switch is vendor-specific, so for ``disabled`` nothing is sent and the
        # endpoint's own default applies.
        if thinking and thinking != "disabled":
            payload["reasoning_effort"] = thinking
        if schema and strict:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": _strict_schema(schema)},
            }
        elif schema:
            payload["messages"][0]["content"] = (
                system
                + "\n\nReturn only a JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        return payload

    def _extract_text(self, raw: dict[str, Any]) -> str:
        return _join_choices(raw)

    def _sent_prompts(self, payload: dict[str, Any]) -> tuple[str, str, int]:
        msgs = payload["messages"]
        return msgs[0]["content"], msgs[1]["content"], payload["max_tokens"]


class ResponsesBackend(_HttpMixin):
    """OpenAI Responses wire format."""

    protocol = "responses"
    _endpoint = "/v1/responses"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.name = f"responses:{self.model}"

    def _payload(self, *, model: str, system: str, user: str, max_tokens: int,
                 thinking: str, schema: dict[str, Any] | None, strict: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_tokens,
        }
        if thinking and thinking != "disabled":
            payload["reasoning"] = {"effort": thinking}
        if schema and strict:
            payload["text"] = {
                "format": {"type": "json_schema", "name": "result",
                           "schema": _strict_schema(schema), "strict": True}
            }
        elif schema:
            payload["instructions"] = (
                system
                + "\n\nReturn only a JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        return payload

    def _extract_text(self, raw: dict[str, Any]) -> str:
        return _join_responses(raw)

    def _sent_prompts(self, payload: dict[str, Any]) -> tuple[str, str, int]:
        return payload.get("instructions", ""), payload.get("input", ""), payload["max_output_tokens"]


#: Model families whose reasoning cannot be switched off: ``thinking.type``
#: ``disabled`` is a 400 on them and effort is the only control. Matched as a
#: prefix on the model id so gateway suffixes (``-official``, ``-awsb``) count.
_ALWAYS_THINKING_PREFIXES = (
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
    "claude-fable-", "claude-mythos-",
)


def _thinking_always_on(model: str) -> bool:
    name = (model or "").lower()
    return any(name.startswith(p) for p in _ALWAYS_THINKING_PREFIXES)


def _join_text_blocks(raw: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in raw.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def _join_choices(raw: dict[str, Any]) -> str:
    choices = raw.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI strict mode requires every property listed as required."""
    result = dict(schema)
    if "properties" in result:
        result["properties"] = {k: _strict_schema(v) for k, v in result["properties"].items()}
        result["required"] = list(result["properties"])
        result["additionalProperties"] = False
    if isinstance(result.get("items"), dict):
        result["items"] = _strict_schema(result["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in result:
            result[key] = [_strict_schema(v) for v in result[key]]
    return result


def _join_responses(raw: dict[str, Any]) -> str:
    if isinstance(raw.get("output_text"), str) and raw["output_text"]:
        return raw["output_text"]
    parts: list[str] = []
    for item in raw.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
    return "".join(parts)


_BACKENDS: dict[str, type[_HttpMixin]] = {
    "messages": MessagesBackend,
    "chat-completions": ChatCompletionsBackend,
    "responses": ResponsesBackend,
}


def build_backend(cfg, *, caps_path: Path | None = None) -> ModelBackend:
    """Instantiate the backend described by :class:`carrel.config.ModelConfig`.

    The per-task table (model / thinking / max_tokens) is resolved here once, so
    the rest of the app only ever names a task. ``caps_path`` is where the
    strict-schema probe result is persisted across processes.
    """
    cls = _BACKENDS.get(cfg.protocol)
    if cls is None:
        raise ValueError(f"unknown model protocol: {cfg.protocol}")
    by_task: dict[str, tuple[str, str, int]] = {}
    for_task = getattr(cfg, "for_task", None)
    if callable(for_task):
        from carrel.config import MODEL_TASKS
        by_task = {task: for_task(task) for task in MODEL_TASKS}
    return cls(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.name,
        timeout_s=cfg.timeout_s,
        max_retries=getattr(cfg, "max_retries", 1),
        thinking=getattr(cfg, "thinking", "disabled"),
        pricing=getattr(cfg, "pricing", None),
        by_task=by_task,
        extra_headers=getattr(cfg, "extra_headers", None),
        caps_path=caps_path,
    )
